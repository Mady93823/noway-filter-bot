"""Index one Telegram message: parse -> resolve title -> insert file row.

Used identically by live indexing and backfill. Duplicate messages are
free: the unique constraint on telegram_file_uid turns them into no-ops.
"""

import logging
from enum import StrEnum

from pyrogram.types import Message

from shared.db.engine import get_session_factory
from shared.db.repos import files as files_repo
from shared.parsing.filename import parse_media
from worker.resolver import resolve_title

logger = logging.getLogger(__name__)


def _extract_media(message: Message):
    return message.document or message.video or message.audio


class IndexOutcome(StrEnum):
    INDEXED = "indexed"      # new file row created
    DUPLICATE = "duplicate"  # exact same Telegram file already indexed
    SKIPPED = "skipped"      # no media / nothing parseable


async def index_message(message: Message) -> IndexOutcome:
    media = _extract_media(message)
    if media is None:
        return IndexOutcome.SKIPPED

    file_name = getattr(media, "file_name", None)
    parsed = parse_media(file_name, message.caption)
    if not parsed.title_guess:
        logger.debug(
            "skipping %s/%s: nothing usable in name/caption",
            message.chat.id,
            message.id,
        )
        return IndexOutcome.SKIPPED

    session_factory = get_session_factory()
    async with session_factory() as session:
        async with session.begin():
            title = await resolve_title(session, parsed)
            inserted = await files_repo.insert_file(
                session,
                title_id=title.id,
                telegram_file_uid=media.file_unique_id,
                telegram_file_id=media.file_id,
                raw_file_name=file_name,
                caption=message.caption,
                quality=parsed.quality,
                file_size=media.file_size,
                mime_type=getattr(media, "mime_type", None),
                source_channel_id=message.chat.id,
                source_message_id=message.id,
                languages=list(parsed.languages),
                episodes=parsed.episodes,
            )
    if inserted:
        logger.info(
            "new index: %r (%s) from %s/%s",
            parsed.title_guess,
            parsed.quality,
            message.chat.id,
            message.id,
        )
        return IndexOutcome.INDEXED
    logger.debug(
        "already indexed: %s/%s (%r)", message.chat.id, message.id, parsed.title_guess
    )
    return IndexOutcome.DUPLICATE
