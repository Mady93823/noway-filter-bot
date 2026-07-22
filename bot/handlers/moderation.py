"""Ban management - admin only, PM only.

Every command writes Postgres first and only then updates the Redis
mirror, so a crash between the two leaves the DB authoritative and the
next refresh_bans() repairs the mirror. The reverse order could drop a
ban entirely.
"""

import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from bot import guards
from shared.config import get_settings
from shared.db.engine import get_session_factory
from shared.db.repos import users as users_repo

logger = logging.getLogger(__name__)


def _target(message: Message) -> tuple[int | None, str]:
    """(user_id, reason) from '/ban <id> [reason]', or a replied-to user."""
    parts = (message.text or "").split()
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id, " ".join(parts[1:])
    if len(parts) < 2:
        return None, ""
    try:
        return int(parts[1]), " ".join(parts[2:])
    except ValueError:
        return None, ""


async def _apply_ban(user_id: int, banned: bool, reason: str | None) -> None:
    session_factory = get_session_factory()
    async with session_factory() as session, session.begin():
        await users_repo.set_banned(session, user_id, banned, reason)
    await guards.sync_ban(user_id, banned)


def register_moderation_handlers(app: Client) -> None:
    admin_ids = get_settings().admin_ids
    if not admin_ids:
        return

    admin_pm = filters.private & filters.user(admin_ids)

    @app.on_message(admin_pm & filters.command("ban"))
    async def _on_ban(client: Client, message: Message) -> None:
        user_id, reason = _target(message)
        if user_id is None:
            await message.reply_text(
                "Usage: <code>/ban &lt;user_id&gt; [reason]</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        if user_id in admin_ids:
            await message.reply_text("🛑 Admins cannot be banned.")
            return

        await _apply_ban(user_id, True, reason or None)
        logger.warning(
            "user %s banned by %s: %s", user_id, message.from_user.id, reason
        )
        await message.reply_text(
            f"🔨 Banned <code>{user_id}</code>."
            + (f"\n📝 {reason}" if reason else "")
            + "\n\nThey now get no response at all — searches, taps and "
            "/start are all ignored.",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(admin_pm & filters.command("unban"))
    async def _on_unban(client: Client, message: Message) -> None:
        user_id, _ = _target(message)
        if user_id is None:
            await message.reply_text(
                "Usage: <code>/unban &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML
            )
            return
        await _apply_ban(user_id, False, None)
        logger.info("user %s unbanned by %s", user_id, message.from_user.id)
        await message.reply_text(
            f"✅ Unbanned <code>{user_id}</code>.", parse_mode=ParseMode.HTML
        )

    @app.on_message(admin_pm & filters.command("banned"))
    async def _on_banned(client: Client, message: Message) -> None:
        session_factory = get_session_factory()
        async with session_factory() as session:
            rows = await users_repo.banned_users(session)
        if not rows:
            await message.reply_text("✅ Nobody is banned.")
            return

        lines = [f"🔨 <b>{len(rows)} banned</b>", ""]
        for user in rows[:50]:
            when = user.banned_at.strftime("%Y-%m-%d") if user.banned_at else "?"
            reason = f" — {user.ban_reason}" if user.ban_reason else ""
            lines.append(f"▫️ <code>{user.id}</code>  <i>{when}</i>{reason}")
        if len(rows) > 50:
            lines.append(f"\n<i>…and {len(rows) - 50} more</i>")
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    @app.on_message(admin_pm & filters.command("unbanall"))
    async def _on_unban_all(client: Client, message: Message) -> None:
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            count = await users_repo.unban_all(session)
        # Rebuild rather than removing one by one - the mirror is derived
        # state, and a rebuild is a single query either way.
        await guards.refresh_bans()
        logger.warning("all bans cleared by %s (%d)", message.from_user.id, count)
        await message.reply_text(f"✅ Cleared {count} ban(s).")
