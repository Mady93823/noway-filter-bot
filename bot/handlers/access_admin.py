"""Admin commands for the log channel, the shortlink gate, and premium.

Everything here writes to bot_settings or users.access_until, so it all
takes effect immediately in both the bot and the worker with no restart
- which is the whole reason those values are not environment variables.

/myplan is the one command in this module that is NOT admin-only: it is
how an ordinary user sees the time left on their clock.

Two deliberate refusals:

- Switching the gate on with no shortener token configured is blocked.
  Otherwise every file tap would hit a gate that can never issue a link,
  and the bot would look broken to everyone at once.
- /showconfig never prints the API token in full, and /setshortener
  deletes the message that carried it. That output gets screenshotted.
"""

import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from bot import access, guards, ui
from shared import logchannel
from shared.config import get_settings
from shared.db.engine import get_session_factory
from shared.db.repos import users as users_repo
from shared.logchannel import log_event
from shared.settings_store import (
    ACCESS_HOURS,
    DEFAULT_SHORTENER_BASE,
    FUN_MODE,
    GATE_ENABLED,
    LOG_CHANNEL,
    POSTER_MODE,
    POSTER_MODE_PHOTO,
    POSTER_MODE_THUMB,
    SHORTENER_API,
    SHORTENER_BASE,
    access_hours,
    clear_setting,
    fun_mode,
    gate_enabled,
    log_channel_id,
    mask_token,
    poster_mode,
    set_setting,
    shortener_config,
)

logger = logging.getLogger(__name__)


def register_access_handlers(app: Client) -> None:
    admin_ids = get_settings().admin_ids
    if not admin_ids:
        logger.warning("ADMIN_IDS is empty - access admin commands disabled")
        return
    admin_pm = filters.private & filters.user(admin_ids)

    @app.on_message(admin_pm & filters.command("setlog"))
    async def _on_setlog(client: Client, message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.reply_text(
                "Usage: <code>/setlog -1001234567890</code> or "
                "<code>/setlog off</code>\n\n"
                "Add this bot to the channel as an admin first.",
                parse_mode=ParseMode.HTML,
            )
            return

        if parts[1].lower() == "off":
            await clear_setting(LOG_CHANNEL)
            await message.reply_text("🔕 Event logging disabled.")
            return

        try:
            channel_id = int(parts[1])
        except ValueError:
            await message.reply_text(
                "Channel id must be a number, e.g. -1001234567890."
            )
            return

        # Prove the bot can actually post there before saving, so a typo
        # fails now, loudly, instead of silently swallowing every event.
        try:
            await client.send_message(channel_id, "✅ Log channel connected.")
        except Exception as exc:
            await message.reply_text(
                f"❌ Can't post there: <code>{exc}</code>\n"
                "Add the bot to that channel as an admin and try again.",
                parse_mode=ParseMode.HTML,
            )
            return

        await set_setting(LOG_CHANNEL, str(channel_id))
        await message.reply_text(
            f"📝 Logging to <code>{channel_id}</code>.", parse_mode=ParseMode.HTML
        )

    @app.on_message(admin_pm & filters.command("setshortener"))
    async def _on_setshortener(client: Client, message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) not in (2, 3):
            await message.reply_text(
                "Usage: <code>/setshortener &lt;api_token&gt; [api_url]</code>\n\n"
                f"Default url: <code>{DEFAULT_SHORTENER_BASE}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        await set_setting(SHORTENER_API, parts[1])
        if len(parts) == 3:
            await set_setting(SHORTENER_BASE, parts[2])
        token, base = await shortener_config()

        # Delete the message carrying the raw token: admin PMs get
        # scrolled through and screenshotted like any other chat.
        try:
            await message.delete()
        except Exception:
            pass

        await client.send_message(
            message.chat.id,
            f"🔗 Shortener saved: <code>{mask_token(token or '')}</code>\n"
            f"Endpoint: <code>{base}</code>\n\n"
            "<i>Your message was deleted so the token isn't left in the chat.</i>",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(admin_pm & filters.command("shortlink"))
    async def _on_shortlink(client: Client, message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
            state = "ON" if await gate_enabled() else "OFF"
            await message.reply_text(
                f"Gate is currently <b>{state}</b>.\n"
                "Usage: <code>/shortlink on</code> | <code>/shortlink off</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        if parts[1].lower() == "on":
            token, _ = await shortener_config()
            if not token:
                await message.reply_text(
                    "❌ Set a shortener token first: "
                    "<code>/setshortener &lt;api_token&gt;</code>\n"
                    "<i>Turning the gate on without one would block every "
                    "download behind a link the bot cannot create.</i>",
                    parse_mode=ParseMode.HTML,
                )
                return
            await set_setting(GATE_ENABLED, "1")
            hours = await access_hours()
            await message.reply_text(
                f"🔒 Gate <b>ON</b> — one unlock buys {hours}h of unlimited files.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await set_setting(GATE_ENABLED, "0")
            await message.reply_text(
                "🔓 Gate <b>OFF</b> — files are free for everyone.",
                parse_mode=ParseMode.HTML,
            )

    @app.on_message(admin_pm & filters.command("setaccesshours"))
    async def _on_sethours(client: Client, message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) < 1:
            await message.reply_text(
                "Usage: <code>/setaccesshours 4</code> (whole hours, minimum 1)",
                parse_mode=ParseMode.HTML,
            )
            return
        await set_setting(ACCESS_HOURS, parts[1])
        await message.reply_text(
            f"⏱ One unlock now grants <b>{parts[1]} hours</b>.\n"
            "<i>Access already granted keeps the length it was given.</i>",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(admin_pm & filters.command("postermode"))
    async def _on_postermode(client: Client, message: Message) -> None:
        parts = (message.text or "").split()
        valid = (POSTER_MODE_PHOTO, POSTER_MODE_THUMB)
        if len(parts) != 2 or parts[1].lower() not in valid:
            current = await poster_mode()
            await message.reply_text(
                f"Poster display is <b>{current}</b>.\n"
                "Usage: <code>/postermode photo</code> | "
                "<code>/postermode thumb</code>\n\n"
                "<blockquote>📷 <b>photo</b> — tapping a title opens a full "
                "poster card (replaces the list; “back” rebuilds it)\n"
                "🖼 <b>thumb</b> — the card stays text with a small poster "
                "preview beneath it</blockquote>\n"
                "<i>Only affects titles that have a fetched poster.</i>",
                parse_mode=ParseMode.HTML,
            )
            return
        await set_setting(POSTER_MODE, parts[1].lower())
        await message.reply_text(
            f"🖼 Poster mode set to <b>{parts[1].lower()}</b>.",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(admin_pm & filters.command("funmode"))
    async def _on_funmode(client: Client, message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
            state = "ON" if await fun_mode() else "OFF"
            await message.reply_text(
                f"Fun voice is <b>{state}</b>.\n"
                "Usage: <code>/funmode on</code> | <code>/funmode off</code>\n\n"
                "<blockquote>😎 <b>on</b> — genz voice: roasts, hype lines, "
                "emoji reactions, easter eggs\n"
                "🎩 <b>off</b> — the plain, straight-faced voice</blockquote>",
                parse_mode=ParseMode.HTML,
            )
            return
        await set_setting(FUN_MODE, "1" if parts[1].lower() == "on" else "0")
        if parts[1].lower() == "on":
            await message.reply_text("😎 Fun mode <b>ON</b> — we vibin now 🔥", parse_mode=ParseMode.HTML)
        else:
            await message.reply_text("🎩 Fun mode <b>OFF</b> — back to business.", parse_mode=ParseMode.HTML)

    @app.on_message(admin_pm & filters.command("addpremium"))
    async def _on_addpremium(client: Client, message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) != 3 or not parts[1].lstrip("-").isdigit():
            await message.reply_text(
                "Usage: <code>/addpremium &lt;user_id&gt; &lt;duration&gt;</code>\n\n"
                "Durations: <code>6h</code> <code>30d</code> <code>2w</code> "
                "<code>1m</code> <code>1y</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        duration = access.parse_duration(parts[2])
        if duration is None:
            await message.reply_text(
                "Bad duration. Use a number plus h/d/w/m/y, e.g. <code>30d</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

        user_id = int(parts[1])
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            expiry = await users_repo.grant_access(session, user_id, duration)

        remaining = access.format_remaining(expiry)
        await message.reply_text(
            f"💎 Granted <b>{parts[2]}</b> to <code>{user_id}</code>.\n"
            f"⏳ Now has <b>{remaining}</b> left.",
            parse_mode=ParseMode.HTML,
        )
        # Tell the user their access changed - they did not ask for it
        # and would otherwise never find out.
        try:
            await client.send_message(
                user_id,
                ui.premium_granted_text(remaining or ""),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.info("could not notify %s of premium: %s", user_id, exc)

        await log_event(
            client,
            logchannel.ACCESS,
            "Premium granted",
            {
                "User": user_id,
                "By admin": message.from_user.id,
                "Duration": parts[2],
                "Expires": expiry.strftime("%Y-%m-%d %H:%M UTC"),
            },
        )

    @app.on_message(admin_pm & filters.command("removepremium"))
    async def _on_removepremium(client: Client, message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
            await message.reply_text(
                "Usage: <code>/removepremium &lt;user_id&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        user_id = int(parts[1])
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            await users_repo.revoke_access(session, user_id)
        await message.reply_text(
            f"🚫 Access cleared for <code>{user_id}</code>.",
            parse_mode=ParseMode.HTML,
        )
        await log_event(
            client,
            logchannel.ACCESS,
            "Premium revoked",
            {"User": user_id, "By admin": message.from_user.id},
        )

    @app.on_message(admin_pm & filters.command("showconfig"))
    async def _on_showconfig(client: Client, message: Message) -> None:
        token, base = await shortener_config()
        channel = await log_channel_id()
        session_factory = get_session_factory()
        async with session_factory() as session:
            active = await users_repo.active_access_count(session)
        await message.reply_text(
            "⚙️ <b>Runtime configuration</b>\n\n"
            "<blockquote>"
            f"📝 <b>Log channel</b>   {channel or 'not set'}\n"
            f"🔒 <b>Gate</b>   {'ON' if await gate_enabled() else 'OFF'}\n"
            f"⏱ <b>Unlock grants</b>   {await access_hours()}h\n"
            f"🔗 <b>Shortener</b>   {mask_token(token) if token else 'not set'}\n"
            f"🌐 <b>Endpoint</b>   {base}\n"
            f"🖼 <b>Poster mode</b>   {await poster_mode()}\n"
            f"😎 <b>Fun voice</b>   {'ON' if await fun_mode() else 'OFF'}\n"
            f"💎 <b>Active access</b>   {active} user(s)"
            "</blockquote>",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(filters.private & filters.command("myplan") & guards.not_banned)
    async def _on_myplan(client: Client, message: Message) -> None:
        session_factory = get_session_factory()
        async with session_factory() as session:
            expiry = await users_repo.get_access_until(session, message.from_user.id)
        await message.reply_text(
            ui.plan_text(access.format_remaining(expiry)), parse_mode=ParseMode.HTML
        )
