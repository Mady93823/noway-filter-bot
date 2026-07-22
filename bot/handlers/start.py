"""/start, menu navigation, and deep-link file delivery.

/start f_<id> is the deep-link payload used when a group user taps a
result button: delivery always happens in PM so groups stay clean.
"""

import logging
from datetime import timedelta

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from bot import access, gate, guards, ui
from bot.delivery import send_file
from shared import logchannel
from shared.db.engine import get_session_factory
from shared.db.repos import users as users_repo
from shared.logchannel import log_event
from shared.settings_store import access_hours

logger = logging.getLogger(__name__)


async def _register_user(user_id: int) -> bool:
    """Upsert the user. True when this is the first time we've seen them.

    "Is this new" is answered by checking the row before inserting, so
    the log line fires once per person rather than once per /start.
    """
    session_factory = get_session_factory()
    async with session_factory() as session, session.begin():
        seen_before = await users_repo.user_exists(session, user_id)
        await users_repo.upsert_user(session, user_id)
    return not seen_before


async def _verify(client: Client, message: Message, token: str) -> None:
    """Redeem a verification token and start the user's access clock."""
    user = message.from_user
    if not await access.redeem_token(token, user.id):
        await message.reply_text(ui.verify_failed_text(), parse_mode=ParseMode.HTML)
        return

    hours = await access_hours()
    session_factory = get_session_factory()
    async with session_factory() as session, session.begin():
        expiry = await users_repo.grant_access(session, user.id, timedelta(hours=hours))

    await message.reply_text(
        ui.access_granted_text(hours, access.format_remaining(expiry) or ""),
        parse_mode=ParseMode.HTML,
    )
    await log_event(
        client,
        logchannel.ACCESS,
        "Access unlocked",
        {
            "User": f"{user.mention} ({user.id})",
            "Route": "shortlink",
            "Duration": f"{hours}h",
            "Expires": expiry.strftime("%Y-%m-%d %H:%M UTC"),
        },
    )


def register_start_handlers(app: Client) -> None:
    @app.on_message(filters.private & filters.command("start") & guards.not_banned)
    async def _on_start(client: Client, message: Message) -> None:
        if await _register_user(message.from_user.id):
            await log_event(
                client,
                logchannel.NEW_USER,
                "New user started the bot",
                {
                    "User": message.from_user.mention,
                    "Id": message.from_user.id,
                    "Username": f"@{message.from_user.username}"
                    if message.from_user.username
                    else None,
                },
            )

        parts = (message.text or "").split(maxsplit=1)
        payload = parts[1].strip() if len(parts) == 2 else ""
        if payload.startswith("verify_"):
            await _verify(client, message, payload[len("verify_") :])
            return
        if payload.startswith("f_") and payload[2:].isdigit():
            if await gate.blocked(client, message.from_user):
                return
            await send_file(
                client,
                message.chat.id,
                int(payload[2:]),
                user=message.from_user,
                source="deeplink",
            )
            return

        await message.reply_text(
            ui.start_text(message.from_user.mention),
            parse_mode=ParseMode.HTML,
            reply_markup=ui.start_keyboard(),
        )

    @app.on_callback_query(filters.regex(r"^(hlp|abt|hom)$"))
    async def _on_menu(client: Client, callback: CallbackQuery) -> None:
        if await guards.is_banned(callback.from_user.id):
            # Callbacks must be answered or the client spins forever.
            await callback.answer()
            return
        screen = callback.data
        if screen == "hlp":
            text, keyboard = ui.help_text(), ui.back_keyboard()
        elif screen == "abt":
            text, keyboard = ui.about_text(), ui.back_keyboard()
        else:
            text, keyboard = (
                ui.start_text(callback.from_user.mention),
                ui.start_keyboard(),
            )
        await callback.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        await callback.answer()
