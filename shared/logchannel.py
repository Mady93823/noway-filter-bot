"""Event log channel - one destination, tagged messages.

An admin points the bot at a channel with /setlog and everything
interesting lands there: new users, file deliveries, successful indexing,
errors, and titles people keep asking for that do not exist yet. One
channel rather than five, with a leading tag on every message, so it is
a single command to configure and Telegram's own search filters by tag.

Two rules inherited from shared/alerts.py, for the same reasons:

1. Logging can never break what it is logging about. Every send is
   guarded; an unset channel, a bot that was kicked, a FloodWait - none
   of them may propagate into a file delivery or an indexing batch.
2. Volume has to be bounded. Deliveries and indexing events fire
   constantly on a busy bot, so the caller decides what is worth a
   message, and repetition-prone events (missing titles) are deduped by
   the caller before they get here.

The bot and the worker both log, and both read the destination from
bot_settings - so /setlog takes effect in the worker with no restart.
"""

import logging
from html import escape

from shared.settings_store import log_channel_id

logger = logging.getLogger(__name__)

# Tags are fixed strings, not free text: an admin scrolling the channel
# scans the emoji, and Telegram search for "#delivery" has to actually
# find every delivery.
NEW_USER = ("🆕", "#newuser")
DELIVERY = ("📤", "#delivery")
INDEXED = ("💾", "#indexed")
ERROR = ("⚠️", "#error")
MISSING = ("🔍", "#missing")
ACCESS = ("🎟", "#access")


async def log_event(client, tag: tuple[str, str], title: str, fields: dict) -> None:
    """Post one tagged event. Never raises - the caller is mid-job.

    fields render as "label: value" lines, values escaped: a filename or
    a user's display name is untrusted input and would otherwise be able
    to inject markup into the log, or simply fail to send.
    """
    try:
        channel = await log_channel_id()
        if channel is None:
            return  # logging not configured - the common case, stay silent
        emoji, hashtag = tag
        lines = [f"{emoji} <b>{escape(title)}</b>  {hashtag}"]
        lines += [
            f"<b>{escape(str(label))}:</b> {escape(str(value))}"
            for label, value in fields.items()
            if value is not None
        ]
        await client.send_message(
            channel,
            "\n".join(lines),
            parse_mode=_html(),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        # Deliberately swallowed and only logged locally: a broken log
        # channel must not take down deliveries or indexing.
        logger.warning("log channel write failed: %s", exc)


def _html():
    # Imported lazily so this module stays importable from tests that
    # never load Pyrogram's enums.
    from pyrogram.enums import ParseMode

    return ParseMode.HTML
