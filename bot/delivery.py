"""File delivery - one place that turns a files row into a sent message."""

import logging

from pyrogram import Client
from pyrogram.enums import ParseMode

from bot import ui
from shared import logchannel
from shared.db.engine import get_session_factory
from shared.logchannel import log_event
from shared.db.repos import files as files_repo
from shared.db.repos import titles as titles_repo

logger = logging.getLogger(__name__)


async def send_file(
    client: Client,
    chat_id: int,
    file_db_id: int,
    *,
    user=None,
    source: str = "direct",
) -> bool:
    """Send an indexed variant by DB id. Returns False if it vanished.

    source labels how the tap arrived ("direct", "deeplink") and goes
    into the log line together with the channel the file was indexed
    from, so the log answers "who got what, from where, by which route".
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        file = await files_repo.get_file(session, file_db_id)
        title = await titles_repo.get_title(session, file.title_id) if file else None
    if file is None or title is None:
        await client.send_message(
            chat_id, "😕 That file is no longer available. Try searching again."
        )
        return False

    await client.send_cached_media(
        chat_id,
        file.telegram_file_id,
        caption=ui.delivery_caption(
            title.display_title,
            title.year,
            # This variant's own audio tracks; title union only as fallback
            # for rows indexed before per-file languages existed.
            tuple(file.languages) or tuple(title.languages),
            file.quality,
            file.file_size,
            title.season,
            file.episodes,
        ),
        parse_mode=ParseMode.HTML,
    )
    await log_event(
        client,
        logchannel.DELIVERY,
        "File delivered",
        {
            "User": f"{user.mention} ({user.id})" if user else chat_id,
            "Title": f"{title.display_title}"
            + (f" ({title.year})" if title.year else ""),
            "Quality": file.quality,
            "Size": ui.format_size(file.file_size),
            "Source channel": file.source_channel_id,
            "Source message": file.source_message_id,
            "Route": source,
        },
    )
    return True
