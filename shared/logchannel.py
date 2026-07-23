"""Event log channel - one destination, tagged messages, rate-safe.

An admin points the bot at a channel with /setlog and everything
interesting lands there: new users, file deliveries, successful indexing,
errors, and titles people keep asking for that do not exist yet. One
channel rather than five, with a leading tag on every message, so it is
a single command to configure and Telegram's own search filters by tag.

Two hard rules, inherited from shared/alerts.py:

1. Logging can never break what it is logging about. Every step is
   guarded; an unset channel, a kicked bot, a FloodWait - none of them
   may propagate into a file delivery or an indexing batch.
2. Volume is bounded, and it all targets ONE chat. The log channel has a
   ~1 msg/sec limit, but deliveries fire far faster than that on a busy
   bot. So `log_event` never sends inline: it renders the line and drops
   it into a small in-process queue, and a single drainer posts them
   through the rate governor. Under a storm the queue fills and the
   newest events are dropped rather than backpressuring the delivery that
   produced them.

The queue is per-process and deliberately not in Redis: a log lost on
restart is just a lost log, which for best-effort telemetry is fine, and
a Redis round trip per delivery-log would add exactly the load we are
avoiding. The durable things (checkpoints, access) live in Postgres and
Redis as the rules require; this is not one of them.

The bot and the worker each start their own drainer and both read the
destination from bot_settings, so /setlog takes effect with no restart.
"""

import asyncio
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

# Bounded so a burst cannot grow memory without limit. At the log
# channel's ~1/sec drain rate this is a few minutes of headroom; past it,
# the newest events are dropped (put_nowait raises) so the queue is never
# a source of unbounded growth or backpressure.
_MAXSIZE = 500

_queue: "asyncio.Queue | None" = None
_drainer_task: "asyncio.Task | None" = None
_dropped = 0


def _render(tag: tuple[str, str], title: str, fields: dict) -> str:
    """Build one escaped HTML log line. Untrusted values (filenames, user
    display names) are escaped so they cannot inject markup or fail send."""
    emoji, hashtag = tag
    lines = [f"{emoji} <b>{escape(title)}</b>  {hashtag}"]
    lines += [
        f"<b>{escape(str(label))}:</b> {escape(str(value))}"
        for label, value in fields.items()
        if value is not None
    ]
    return "\n".join(lines)


async def log_event(client, tag: tuple[str, str], title: str, fields: dict) -> None:
    """Queue one tagged event for the log channel. Never raises, never blocks.

    `client` is kept for call-site compatibility; the actual send happens
    in the drainer, which holds its own client reference.
    """
    global _dropped
    try:
        if _queue is None:
            return  # drainer not started (tests, or logging never used)
        channel = await log_channel_id()
        if channel is None:
            return  # logging not configured - the common case, stay silent
        line = _render(tag, title, fields)
    except Exception as exc:
        logger.warning("log render failed: %s", exc)
        return
    try:
        _queue.put_nowait((channel, line))
    except asyncio.QueueFull:
        _dropped += 1
        # Note it occasionally, not per drop - the whole point is to not
        # add work under load.
        if _dropped % 100 == 1:
            logger.warning("log channel saturated - %s events dropped so far", _dropped)


async def _drain(client) -> None:
    assert _queue is not None
    while True:
        channel, line = await _queue.get()
        try:
            # client is governed: this send waits on the global + per-chat
            # buckets, so it can never exceed the log channel's 1/sec and
            # it counts against the same global budget as deliveries.
            await client.send_message(
                channel, line, parse_mode=_html(), disable_web_page_preview=True
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A broken log channel must not take down the drainer.
            logger.warning("log channel write failed: %s", exc)
        finally:
            _queue.task_done()


def start_log_drainer(client) -> "asyncio.Task":
    """Start the background drainer. Call once, after the client starts and
    the governor is installed. Returns the task so the caller can cancel it."""
    global _queue, _drainer_task
    if _drainer_task is not None:
        return _drainer_task
    _queue = asyncio.Queue(maxsize=_MAXSIZE)
    _drainer_task = asyncio.create_task(_drain(client))
    logger.info("log channel drainer started")
    return _drainer_task


async def stop_log_drainer() -> None:
    global _queue, _drainer_task
    if _drainer_task is not None:
        _drainer_task.cancel()
        _drainer_task = None
    _queue = None


def _html():
    # Imported lazily so this module stays importable from tests that
    # never load Pyrogram's enums.
    from pyrogram.enums import ParseMode

    return ParseMode.HTML
