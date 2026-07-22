"""Live auto-indexing: new posts in source channels are indexed as they
arrive (docs.md section 6). Handler only - all real work is in indexer.

A channel counts as a source if it is listed in SOURCE_CHANNEL_IDS *or*
has ever been registered for indexing (an index_progress row exists) -
forwarding a post to the bot is enough, no env edit required.
"""

import logging

from pyrogram import Client, filters
from pyrogram.types import Message

from shared import logchannel
from shared.config import get_settings
from shared.db.engine import get_session_factory
from shared.db.repos import progress as progress_repo
from shared.logchannel import log_event
from worker.indexer import IndexOutcome, index_message

logger = logging.getLogger(__name__)


def _size_label(size: int | None) -> str:
    """Human file size for the log line.

    Local rather than imported from bot.ui: the worker must not depend on
    the bot package - they are separate services that share only shared/.
    """
    if not size:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.2f} GB"


async def _is_source_channel(channel_id: int) -> bool:
    if channel_id in get_settings().source_channel_ids:
        return True
    session_factory = get_session_factory()
    async with session_factory() as session:
        return await progress_repo.get_job(session, channel_id) is not None


def register_live_handlers(app: Client) -> None:
    media_filter = filters.channel & (
        filters.document | filters.video | filters.audio
    )

    @app.on_message(media_filter)
    async def _on_channel_media(client: Client, message: Message) -> None:
        try:
            if not await _is_source_channel(message.chat.id):
                logger.debug("ignoring media from unregistered %s", message.chat.id)
                return
            outcome = await index_message(message)
            # Only the live path logs to the channel, and only for files
            # that were actually new. index_message is shared with
            # backfill, where this would post one message per file for
            # lakhs of files - the unbounded log-channel growth CLAUDE.md
            # calls out as an anti-pattern.
            if outcome is IndexOutcome.INDEXED:
                media = message.document or message.video or message.audio
                await log_event(
                    client,
                    logchannel.INDEXED,
                    "New file indexed",
                    {
                        "File": getattr(media, "file_name", None) or "(no filename)",
                        "Size": _size_label(getattr(media, "file_size", None)),
                        "Channel": f"{message.chat.title or ''} "
                        f"({message.chat.id})".strip(),
                        "Message": message.id,
                    },
                )
        except Exception as exc:
            logger.exception(
                "live indexing failed for %s/%s", message.chat.id, message.id
            )
            await log_event(
                client,
                logchannel.ERROR,
                "Live indexing failed",
                {
                    "Channel": message.chat.id,
                    "Message": message.id,
                    "Error": f"{type(exc).__name__}: {exc}",
                },
            )
