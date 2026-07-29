"""File delivery - one place that turns a files row into a sent message."""

import logging

from pyrogram import Client
from pyrogram.enums import ParseMode

from bot import engagement, fun, ui
from bot.ephemeral import schedule_delivery_expiry
from shared import logchannel, settings_store
from shared.config import get_settings
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

    # Funmode caption sign-off: a download milestone / first-of-day nod wins
    # over the random hype line; None in plain mode falls back to "Enjoy".
    hype = None
    if await settings_store.fun_mode():
        recipient = user.id if user is not None else chat_id
        hype = await engagement.touch_download(recipient) or fun.delivery_hype(True)

    sent = await client.send_cached_media(
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
            hype=hype,
        ),
        parse_mode=ParseMode.HTML,
    )

    # The file is temporary, so the warning has to arrive with it - not be
    # something the user could have read somewhere else. Both message ids
    # go into Redis together: the sweeper deletes the file and rewrites
    # this notice in place, so the chat explains what happened instead of
    # quietly losing a message.
    ttl = get_settings().delivery_ttl
    try:
        notice = await client.send_message(
            chat_id,
            ui.delivery_warning_text(ttl),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=sent.id,
        )
        notice_id = notice.id
    except Exception as exc:
        # An unsent warning must not cost the user their file. Schedule
        # the deletion anyway with no notice to rewrite (id 0).
        logger.warning("delivery notice failed for %s: %s", chat_id, exc)
        notice_id = 0
    await schedule_delivery_expiry(chat_id, sent.id, notice_id, ttl)

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
