"""Bot service entrypoint.

Handler registration order matters - Pyrogram runs the first matching
handler and stops:

    admin, moderation   PM commands; forwarded posts must never be
                        treated as searches
    start               /start and the menu
    groups              join/leave bookkeeping (service messages only)
    group_filters       keyword replies; falls through via
                        continue_propagation() when nothing matches
    callbacks           button taps (a separate update type)
    search              the plain-text catch-all, deliberately last

This process never runs indexing work itself - it only creates job rows
that the worker picks up.
"""

import asyncio
import logging

from pyrogram import idle

from bot import guards
from bot.ephemeral import sweeper
from bot.handlers.access_admin import register_access_handlers
from bot.handlers.admin import register_admin_handlers
from bot.handlers.callbacks import register_callback_handlers
from bot.handlers.group_filters import register_group_filter_handlers
from bot.handlers.groups import register_group_handlers
from bot.handlers.moderation import register_moderation_handlers
from bot.handlers.search import register_search_handlers
from bot.handlers.start import register_start_handlers
from shared.alerts import notify_admins
from shared.config import get_settings
from shared.db.engine import dispose_engine
from shared.health import start_health_server
from shared.logchannel import start_log_drainer, stop_log_drainer
from shared.ratelimit import install_governor
from shared.telegram.client import create_client
from shared.watchdog import watchdog

logger = logging.getLogger(__name__)


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = create_client("nowaybot-bot")
    register_admin_handlers(app)
    register_access_handlers(app)
    register_moderation_handlers(app)
    register_start_handlers(app)
    register_group_handlers(app)
    register_group_filter_handlers(app)
    register_callback_handlers(app)
    register_search_handlers(app)
    await app.start()
    # Route every outbound send through the shared rate governor before
    # anything can send - deliveries, replies, logs and deletes all draw
    # from the same per-token budget the worker also respects.
    install_governor(app)
    # Warm the ban mirror before serving: a cold Redis must not mean a
    # window where every banned user is answered again.
    await guards.refresh_bans()
    health = await start_health_server(get_settings().health_port, "bot")
    # Logs go through a bounded queue drained at the governor's pace, so a
    # delivery storm can never flood the single log chat or backpressure
    # the delivery that produced the log line.
    start_log_drainer(app)
    # Group messages are temporary; the queue is in Redis, so this loop
    # also clears anything left pending by a previous run.
    sweeper_task = asyncio.create_task(sweeper(app))
    # Watch Postgres and Redis; DM admins the moment either drops or
    # recovers, so an outage is not something you find out from users.
    watchdog_task = asyncio.create_task(
        watchdog(app, get_settings().watchdog_interval)
    )
    logger.info("bot started")
    try:
        await idle()
    except Exception as exc:
        logger.exception("bot fatal error")
        await notify_admins(
            app,
            f"🛑 Bot is going down.\n{type(exc).__name__}: {exc}",
            dedupe_key="bot-fatal",
        )
        raise
    finally:
        sweeper_task.cancel()
        watchdog_task.cancel()
        await stop_log_drainer()
        health.close()
        await app.stop()
        await dispose_engine()


def main() -> None:
    asyncio.run(run())
