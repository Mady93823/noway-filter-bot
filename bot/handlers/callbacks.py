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

from bot import access, gate, guards, ui
from bot.delivery import send_file
from shared.config import get_settings
from shared.db.engine import get_session_factory
from shared.db.repos import titles as titles_repo
from shared.db.repos import users as users_repo
from shared.parsing.languages import canonical_language
from shared.search.service import search

logger = logging.getLogger(__name__)


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
        cursor = callback.data[len("nav:") :]
        session_factory = get_session_factory()
        async with session_factory() as session:
            page = await search(session, "", cursor=cursor)

        if page.expired or not page.results:
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

    @app.on_callback_query(filters.regex(r"^t:\d+:"))
    async def _on_title(client: Client, callback: CallbackQuery) -> None:
        if await _blocked(callback):
            return
        # t:<title_id>:<qhash>:<offset>[:<lang_code>]
        parts = callback.data.split(":")
        title_id, cursor = int(parts[1]), f"{parts[2]}:{parts[3]}"
        # Chips carry a 3-letter code; resolve it back through the same
        # dictionary the indexer used, so an unknown code just means "all".
        language = canonical_language(parts[4]) if len(parts) > 4 else None

        session_factory = get_session_factory()
        async with session_factory() as session:
            page = await search(session, "", cursor=cursor)

        result = next((r for r in page.results if r.title_id == title_id), None)
        if page.expired or result is None:
            await callback.answer(ui.expired_text(), show_alert=True)
            return

        text, keyboard = ui.build_title(result, cursor, language=language)
        try:
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
            await callback.answer(
                url=f"https://t.me/{username}?start=f_{file_db_id}"
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
        try:
            await callback.message.delete()
        except Exception:  # message may already be gone
            pass
        await callback.answer()
