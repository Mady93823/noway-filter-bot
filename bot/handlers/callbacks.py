"""Result-message callbacks: page turns, title taps, file taps, close.

Delivery rule: in PM the file is sent right there; in a group the tap
opens the bot's PM via deep link so the group stays clean.

Opening a title re-runs the cached page rather than loading that title
on its own: the page carries the query's quality/language filters, so
the variant list a user opens is exactly the one the search promised,
and an expired cache stays handled in one place.
"""

import logging

from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery

from bot import access, gate, guards, ownership, ui
from bot.delivery import send_file
from bot.ephemeral import schedule_delete
from shared import settings_store
from shared.config import get_settings
from shared.db.engine import get_session_factory
from shared.db.repos import titles as titles_repo
from shared.db.repos import users as users_repo
from shared.parsing.languages import canonical_language
from shared.search.service import search

logger = logging.getLogger(__name__)


async def _retrack_group_expiry(callback: CallbackQuery, new_msg) -> None:
    """Re-arm auto-delete AND ownership after a card is REPLACED, not edited.

    A text<->photo transition cannot be an edit, so those paths send a fresh
    message and drop the old one - a new message id the old schedule and the
    old owner record both miss. Re-point both at the replacement on the same
    TTL: whoever navigated here is by definition the card's owner (the guard
    let them through). A no-op in PM, where nothing self-deletes or is owned.
    """
    if new_msg is None or callback.message.chat.type == ChatType.PRIVATE:
        return
    ttl = get_settings().group_message_ttl
    await schedule_delete(new_msg.chat.id, new_msg.id, ttl)
    if callback.from_user is not None:
        await ownership.remember(
            new_msg.chat.id, new_msg.id, callback.from_user.id, ttl
        )


async def _owns_card(callback: CallbackQuery) -> bool:
    """Whether this user may drive this card's buttons.

    Always true in PM (one user) and for admins (they can touch anything).
    In a group, true only for the user who opened the card - looked up in
    Redis, fail-open when the owner is unknown (see bot.ownership).
    """
    msg = callback.message
    if not msg or msg.chat.type == ChatType.PRIVATE:
        return True
    user_id = callback.from_user.id if callback.from_user else None
    if user_id is not None and user_id in get_settings().admin_ids:
        return True
    owner = await ownership.owner_of(msg.chat.id, msg.id)
    return owner is None or owner == user_id


def register_callback_handlers(app: Client) -> None:
    async def _blocked(callback: CallbackQuery) -> bool:
        """Banned users get a silent, answered callback.

        A callback query must be answered or the client shows a spinner
        until it times out - so unlike messages, silence here means an
        empty answer, not no answer at all.
        """
        if not await guards.is_banned(callback.from_user.id):
            return False
        await callback.answer()
        return True

    @app.on_callback_query(filters.regex(r"^dym:\d+$"))
    async def _on_suggestion(client: Client, callback: CallbackQuery) -> None:
        """Tapping a "did you mean" candidate runs it as a normal search.

        Searching the title's own canonical text rather than opening the
        title row directly is what gives the result a real cursor - so
        the card that comes back paginates and filters like any other,
        instead of being a special case half the keyboard ignores.
        """
        if await _blocked(callback):
            return
        if not await _owns_card(callback):
            await callback.answer(ui.not_your_card_alert(), show_alert=True)
            return
        title_id = int(callback.data[len("dym:") :])
        session_factory = get_session_factory()
        async with session_factory() as session:
            title = await titles_repo.get_title(session, title_id)
            if title is None:
                await callback.answer(ui.expired_text(), show_alert=True)
                return
            page = await search(session, title.canonical_title)

        if not page.results:
            await callback.answer(ui.expired_text(), show_alert=True)
            return

        text, keyboard = ui.build_results(page, get_settings().search_page_size)
        try:
            await callback.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
        except MessageNotModified:
            pass
        await callback.answer()

    @app.on_callback_query(filters.regex(r"^nav:"))
    async def _on_nav(client: Client, callback: CallbackQuery) -> None:
        if await _blocked(callback):
            return
        if not await _owns_card(callback):
            await callback.answer(ui.not_your_card_alert(), show_alert=True)
            return
        cursor = callback.data[len("nav:") :]
        session_factory = get_session_factory()
        async with session_factory() as session:
            page = await search(session, "", cursor=cursor)

        if page.expired or not page.results:
            await callback.answer(ui.expired_text(), show_alert=True)
            return

        text, keyboard = ui.build_results(page, get_settings().search_page_size)
        msg = callback.message
        try:
            if msg and msg.photo:
                # Returning to the list from a photo detail: a text list
                # cannot be an edit of a photo message, so send it fresh and
                # drop the photo. (Page turns arrive here too, but on a text
                # list - those still take the plain edit branch below.)
                sent = await client.send_message(
                    msg.chat.id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
                await _retrack_group_expiry(callback, sent)
                try:
                    await msg.delete()
                except Exception:
                    pass
            else:
                await callback.edit_message_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
        except MessageNotModified:
            pass
        await callback.answer()

    @app.on_callback_query(filters.regex(r"^t:\d+:"))
    async def _on_title(client: Client, callback: CallbackQuery) -> None:
        if await _blocked(callback):
            return
        if not await _owns_card(callback):
            await callback.answer(ui.not_your_card_alert(), show_alert=True)
            return
        # t:<title_id>:<qhash>:<offset>[:<lang>[:<res>:<page>]] - see the
        # grammar in bot.ui. The short forms are not just legacy: an
        # unfiltered first page still emits them, so both stay live.
        parts = callback.data.split(":")
        title_id, cursor = int(parts[1]), f"{parts[2]}:{parts[3]}"
        # Chips carry a 3-letter code; resolve it back through the same
        # dictionary the indexer used, so an unknown code just means "all".
        language = (
            canonical_language(parts[4])
            if len(parts) > 4 and parts[4] != ui.NO_FILTER
            else None
        )
        quality = parts[5] if len(parts) > 5 and parts[5] != ui.NO_FILTER else None
        # A malformed page renders as page 1 rather than being refused -
        # build_title clamps it against the real page count regardless.
        variant_page = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 0
        # Episode INDEX into the title's episode list ('-'/absent = all).
        # build_title clamps an out-of-range index back to "all".
        episode = int(parts[7]) if len(parts) > 7 and parts[7].isdigit() else None

        session_factory = get_session_factory()
        async with session_factory() as session:
            page = await search(session, "", cursor=cursor)

        result = next((r for r in page.results if r.title_id == title_id), None)
        if page.expired or result is None:
            await callback.answer(ui.expired_text(), show_alert=True)
            return

        # Sibling seasons on this same page power the season switcher; they
        # must come from page.results because switching reuses their t:
        # callback, which only resolves inside the cached page.
        seasons = ui.season_siblings(page.results, result)
        text, keyboard = ui.build_title(
            result, cursor, language=language, quality=quality, page=variant_page,
            episode=episode, seasons=seasons,
        )
        mode = await settings_store.poster_mode()
        msg = callback.message
        try:
            if msg and msg.photo:
                # Already a photo card (a lone hit, or a detail we spawned):
                # a photo message has no text body, so its chip/page updates
                # edit the caption. The poster image is fixed at creation -
                # switching a season sibling updates the caption, not the art.
                await callback.edit_message_caption(
                    text, parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
            elif result.poster_url and mode == settings_store.POSTER_MODE_PHOTO:
                # Text list -> real photo detail. Telegram cannot edit a text
                # message into a photo, so send a fresh photo card and drop
                # the list; "back" rebuilds the list (see _on_nav). A dead
                # poster URL falls back to editing the text in place.
                sent = None
                try:
                    sent = await client.send_photo(
                        msg.chat.id, result.poster_url, caption=text,
                        parse_mode=ParseMode.HTML, reply_markup=keyboard,
                    )
                except Exception as exc:
                    logger.warning("poster card send failed (%s); text fallback", exc)
                if sent is not None:
                    await _retrack_group_expiry(callback, sent)
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                else:
                    await callback.edit_message_text(
                        text, parse_mode=ParseMode.HTML, reply_markup=keyboard
                    )
            elif result.poster_url and mode == settings_store.POSTER_MODE_THUMB:
                # Text card stays text; the poster rides along as a link
                # preview, so the open (and every chip edit after it) is a
                # plain in-place text edit.
                await callback.edit_message_text(
                    ui.with_poster_preview(text, result.poster_url),
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
            else:
                await callback.edit_message_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
        except MessageNotModified:
            pass
        await callback.answer()

    @app.on_callback_query(filters.regex(r"^get:\d+$"))
    async def _on_get(client: Client, callback: CallbackQuery) -> None:
        if await _blocked(callback):
            return
        file_db_id = int(callback.data[len("get:") :])

        if callback.message and callback.message.chat.type != ChatType.PRIVATE:
            username = client.me.username
            # g_ (not f_) so the PM handoff is logged as Route "group"
            # rather than a generic shared deeplink - the log then tells a
            # group tap apart from a link someone pasted elsewhere.
            await callback.answer(
                url=f"https://t.me/{username}?start=g_{file_db_id}"
            )
            return

        if await gate.blocked(client, callback.from_user, callback.message):
            await callback.answer()
            return

        await callback.answer("📤 Sending…")
        await send_file(
            client, callback.from_user.id, file_db_id, user=callback.from_user
        )

    @app.on_callback_query(filters.regex(r"^plan$"))
    async def _on_plan(client: Client, callback: CallbackQuery) -> None:
        if await _blocked(callback):
            return
        session_factory = get_session_factory()
        async with session_factory() as session:
            expiry = await users_repo.get_access_until(session, callback.from_user.id)
        await callback.answer(
            ui.plan_alert(access.format_remaining(expiry)), show_alert=True
        )

    @app.on_callback_query(filters.regex(r"^nop$"))
    async def _on_noop(client: Client, callback: CallbackQuery) -> None:
        """The page counter is a label, not a control.

        It still has to be answered - an unanswered callback spins on the
        client until it times out.
        """
        await callback.answer()

    @app.on_callback_query(filters.regex(r"^x$"))
    async def _on_close(client: Client, callback: CallbackQuery) -> None:
        if not await _owns_card(callback):
            await callback.answer(ui.not_your_card_alert(), show_alert=True)
            return
        try:
            await callback.message.delete()
        except Exception:  # message may already be gone
            pass
        await callback.answer()
