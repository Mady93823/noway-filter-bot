"""Admin-only handlers: backfill jobs, /help, /stats, /clear_index.

Two ways to start indexing a channel:
1. Forward the channel's latest post to the bot in PM.
2. /index <channel_id> <last_message_id>

Both just upsert an index_progress row - the worker does everything else.
"""

import logging
import platform
import time
from pathlib import Path

import psutil
from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.ui import format_size
from shared.config import get_settings
from shared.db.engine import get_session_factory
from shared.db.repos import filters as filters_repo
from shared.db.repos import groups as groups_repo
from shared.db.repos import progress as progress_repo
from shared.db.repos import stats as stats_repo
from shared.db.repos import users as users_repo
from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

_STARTED_AT = time.monotonic()

_HELP_TEXT = (
    "🛠 <b>Admin commands</b>\n\n"
    "▫️ <b>Forward a channel post</b> — start indexing that channel up to "
    "the forwarded message\n"
    "▫️ <code>/index &lt;channel_id&gt; &lt;last_message_id&gt;</code> — same, by hand\n"
    "▫️ <code>/stats</code> — index counts, DB/Redis size, system specs\n"
    "▫️ <code>/clear_index</code> — wipe ALL indexed titles/files (asks to confirm)\n"
    "▫️ <code>/help</code> — this list\n\n"
    "🔨 <b>Moderation</b>\n"
    "▫️ <code>/ban &lt;user_id&gt; [reason]</code> — silence a user everywhere\n"
    "▫️ <code>/unban &lt;user_id&gt;</code> · <code>/banned</code> · "
    "<code>/unbanall</code>\n\n"
    "📝 <b>Log channel</b>\n"
    "▫️ <code>/setlog &lt;channel_id&gt;</code> — send events to a channel "
    "(bot must be admin there; it posts a test message before saving)\n"
    "▫️ <code>/setlog off</code> — stop logging\n"
    "<i>Logged: new users, deliveries (with source channel), indexing, "
    "errors, and titles 3+ people asked for that aren't indexed.</i>\n\n"
    "🔗 <b>Shortlink gate</b>\n"
    "▫️ <code>/setshortener &lt;api_token&gt; [api_url]</code> — AroLinks or "
    "similar (your message is deleted afterwards)\n"
    "▫️ <code>/shortlink on</code> · <code>/shortlink off</code> — gate file "
    "delivery behind one unlock link\n"
    "▫️ <code>/setaccesshours &lt;n&gt;</code> — how long one unlock lasts "
    "(default 4)\n"
    "▫️ <code>/showconfig</code> — current settings (token masked)\n\n"
    "💎 <b>Premium</b>\n"
    "▫️ <code>/addpremium &lt;user_id&gt; &lt;6h|30d|2w|1m|1y&gt;</code> — "
    "extends existing time, never shortens it\n"
    "▫️ <code>/removepremium &lt;user_id&gt;</code>\n"
    "▫️ <code>/myplan</code> — any user checks their own remaining time\n\n"
    "🛠 <b>In groups</b> (group admins)\n"
    "▫️ <code>/filter &lt;keyword&gt; &lt;reply&gt;</code> — canned reply for a keyword\n"
    "▫️ <code>/filters</code> · <code>/stop &lt;keyword&gt;</code> · "
    "<code>/stopall</code>\n\n"
    "👥 <b>User side</b>: /start greeting, plain-text search in PM and groups, "
    "tap a title then a file for delivery."
)


def _uptime() -> str:
    seconds = int(time.monotonic() - _STARTED_AT)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h {minutes}m {secs}s"


async def _stats_text() -> str:
    session_factory = get_session_factory()
    async with session_factory() as session:
        counts = await stats_repo.index_counts(session)
        media_bytes = await stats_repo.total_file_bytes(session)
        jobs = await stats_repo.job_rows(session)
        db_bytes = await stats_repo.db_size_bytes(session)
        groups = await groups_repo.group_counts(session)
        filter_total = await filters_repo.filter_count(session)
        banned = len(await users_repo.banned_ids(session))

    redis_info = await get_redis().info("memory")
    redis_mem = redis_info.get("used_memory_human", "?")

    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(Path(__file__).anchor or "/")

    lines = [
        "📊 <b>Bot statistics</b>",
        "",
        "🗂 <b>Index</b>",
        f"   🎬 Titles: <b>{counts['titles']:,}</b>",
        f"   📁 Files: <b>{counts['files']:,}</b> ({format_size(media_bytes)} of media)",
        f"   🐘 Postgres size: {format_size(db_bytes)}",
        f"   🔴 Redis memory: {redis_mem}",
        "",
        "👥 <b>Reach</b>",
        f"   👤 Users: <b>{counts['users']:,}</b>"
        + (f" (<b>{banned:,}</b> banned)" if banned else ""),
        f"   💬 Groups: <b>{groups['active']:,}</b> active"
        + (
            f" of {groups['total']:,}"
            if groups["total"] != groups["active"]
            else ""
        ),
        f"   🛠 Keyword filters: <b>{filter_total:,}</b>",
        "",
        "⚙️ <b>Indexing jobs</b>",
    ]
    if jobs:
        for job in jobs:
            done = job.last_processed_message_id
            target = job.target_message_id
            percent = (done / target * 100) if target else 0.0
            lines.append(
                f"   • <code>{job.channel_id}</code> — {job.status} "
                f"({done:,}/{target:,} · {percent:.0f}%)"
            )
    else:
        lines.append("   • none yet")
    lines += [
        "",
        "🖥 <b>System</b>",
        f"   OS: {platform.system()} {platform.release()}",
        # interval=None: non-blocking sample since the previous call -
        # never block the event loop inside a handler (golden rule 2).
        f"   CPU: {psutil.cpu_count(logical=True)} threads @ {psutil.cpu_percent(interval=None):.0f}%",
        f"   RAM: {format_size(memory.used)} / {format_size(memory.total)} ({memory.percent:.0f}%)",
        f"   Disk: {format_size(disk.used)} / {format_size(disk.total)} ({disk.percent:.0f}%)",
        f"   Bot uptime: {_uptime()}",
    ]
    return "\n".join(lines)


def _forward_source(message: Message):
    """Channel + message id a message was forwarded from, across API versions."""
    chat = getattr(message, "forward_from_chat", None)
    message_id = getattr(message, "forward_from_message_id", None)
    if chat is None:
        origin = getattr(message, "forward_origin", None)
        chat = getattr(origin, "chat", None)
        message_id = getattr(origin, "message_id", None)
    return chat, message_id


async def _create_job(channel_id: int, target_message_id: int) -> None:
    session_factory = get_session_factory()
    async with session_factory() as session, session.begin():
        await progress_repo.upsert_job(session, channel_id, target_message_id)


def register_admin_handlers(app: Client) -> None:
    admin_ids = get_settings().admin_ids
    if not admin_ids:
        logger.warning("ADMIN_IDS is empty - admin commands disabled")
        return

    admin_pm = filters.private & filters.user(admin_ids)

    @app.on_message(admin_pm & filters.forwarded)
    async def _on_forwarded(client: Client, message: Message) -> None:
        chat, forwarded_id = _forward_source(message)
        if chat is None or forwarded_id is None or chat.type != ChatType.CHANNEL:
            await message.reply_text(
                "Forward a post from a channel (with 'Forwarded from' visible) "
                "to start indexing it."
            )
            return
        await _create_job(chat.id, forwarded_id)
        await message.reply_text(
            f"Indexing job created for {chat.title} ({chat.id}) "
            f"up to message {forwarded_id}.\n"
            "The worker resumes it automatically. Make sure this bot is an "
            "admin in that channel."
        )

    @app.on_message(admin_pm & filters.command("index"))
    async def _on_index_command(client: Client, message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) != 3:
            await message.reply_text(
                "Usage: /index <channel_id> <last_message_id>\n"
                "Or simply forward the channel's latest post to me."
            )
            return
        try:
            channel_id, last_message_id = int(parts[1]), int(parts[2])
        except ValueError:
            await message.reply_text("Both arguments must be integers.")
            return
        await _create_job(channel_id, last_message_id)
        await message.reply_text(
            f"Indexing job created for {channel_id} up to message "
            f"{last_message_id}. The worker will pick it up shortly."
        )

    @app.on_message(admin_pm & filters.command("help"))
    async def _on_help(client: Client, message: Message) -> None:
        await message.reply_text(_HELP_TEXT, parse_mode=ParseMode.HTML)

    @app.on_message(admin_pm & filters.command("stats"))
    async def _on_stats(client: Client, message: Message) -> None:
        await message.reply_text(await _stats_text(), parse_mode=ParseMode.HTML)

    @app.on_message(admin_pm & filters.command("clear_index"))
    async def _on_clear_index(client: Client, message: Message) -> None:
        session_factory = get_session_factory()
        async with session_factory() as session:
            counts = await stats_repo.index_counts(session)
        await message.reply_text(
            "⚠️ <b>Wipe the entire index?</b>\n\n"
            f"This deletes <b>{counts['titles']:,}</b> titles and "
            f"<b>{counts['files']:,}</b> files, plus all indexing progress. "
            "Users are kept. Channels must be re-indexed from scratch.\n\n"
            "This cannot be undone.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🗑 Yes, wipe it", callback_data="clr:yes"),
                        InlineKeyboardButton("✖️ Cancel", callback_data="clr:no"),
                    ]
                ]
            ),
        )

    @app.on_callback_query(filters.regex(r"^clr:(yes|no)$") & filters.user(admin_ids))
    async def _on_clear_confirm(client: Client, callback: CallbackQuery) -> None:
        if callback.data == "clr:no":
            await callback.edit_message_text("Cancelled. Index untouched. ✅")
            await callback.answer()
            return

        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            counts = await stats_repo.wipe_index(session)
        redis = get_redis()
        keys = await redis.keys("search:*")
        if keys:
            await redis.delete(*keys)
        logger.warning(
            "index wiped by admin %s: %s titles, %s files",
            callback.from_user.id,
            counts["titles"],
            counts["files"],
        )
        await callback.edit_message_text(
            f"🗑 Index cleared: <b>{counts['titles']:,}</b> titles, "
            f"<b>{counts['files']:,}</b> files, all jobs and cached searches. "
            "Forward a channel post to reindex.",
            parse_mode=ParseMode.HTML,
        )
        await callback.answer("Wiped")
