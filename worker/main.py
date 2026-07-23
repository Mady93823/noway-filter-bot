"""Worker entrypoint: live indexing + auto-resumed backfills.

On startup, every index_progress row with status 'running' is resumed
from its checkpoint automatically (golden rule 7) - no admin command.
"""

import asyncio
import logging

from pyrogram import Client, idle

from shared.alerts import notify_admins
from shared.config import get_settings
from shared.db.engine import dispose_engine, get_session_factory
from shared.health import start_health_server
from shared.logchannel import start_log_drainer, stop_log_drainer
from shared.ratelimit import install_governor
from shared.db.repos import progress as progress_repo
from shared.telegram.client import create_client
from worker.backfill import run_backfill
from worker.live import register_live_handlers

logger = logging.getLogger(__name__)


async def _report_crashed(client: Client, channel_id: int, task: asyncio.Task) -> None:
    """A finished task may have finished by dying. Say so.

    Without this the dispatcher just restarts it on the next poll, so a
    backfill could crash-loop forever with nobody told. Deduped per
    channel, so the loop DMs once per cooldown rather than once per poll.
    """
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is None:
        return
    logger.error("backfill for %s crashed", channel_id, exc_info=error)
    await notify_admins(
        client,
        f"💥 Backfill for {channel_id} crashed and is being restarted.\n"
        f"{type(error).__name__}: {error}",
        dedupe_key=f"backfill:{channel_id}",
    )


async def job_dispatcher(client: Client) -> None:
    """Poll index_progress and keep one backfill task per running job.

    Job state lives in the DB; this dict only holds asyncio task handles
    and is rebuilt from the DB after any restart.
    """
    session_factory = get_session_factory()
    interval = get_settings().job_poll_interval
    tasks: dict[int, asyncio.Task] = {}
    while True:
        try:
            async with session_factory() as session:
                jobs = await progress_repo.list_running(session)
            for job in jobs:
                existing = tasks.get(job.channel_id)
                if existing is not None and existing.done():
                    await _report_crashed(client, job.channel_id, existing)
                if existing is None or existing.done():
                    logger.info(
                        "starting backfill for channel %s from message %s",
                        job.channel_id,
                        job.last_processed_message_id + 1,
                    )
                    tasks[job.channel_id] = asyncio.create_task(
                        run_backfill(client, job.channel_id)
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A silently dead dispatcher means indexing stops while the
            # worker still looks healthy - the worst failure mode here.
            logger.exception("job dispatcher iteration failed")
            await notify_admins(
                client,
                "⚠️ Worker job dispatcher error — indexing may be stalled.\n"
                f"{type(exc).__name__}: {exc}",
                dedupe_key="dispatcher",
            )
        await asyncio.sleep(interval)


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    app = create_client("nowaybot-worker")
    register_live_handlers(app)
    await app.start()
    # Same governor and log drainer as the bot: the worker's INDEXED and
    # ERROR logs and its progress DMs share the one per-token budget, so
    # a busy indexing run can never crowd out user deliveries.
    install_governor(app)
    health = await start_health_server(settings.health_port, "worker")
    start_log_drainer(app)
    logger.info("worker started")
    dispatcher = asyncio.create_task(job_dispatcher(app))
    try:
        await idle()
    except Exception as exc:
        # Last line of defence: the process is going down either way, but
        # an admin should hear it from the bot, not from a user.
        logger.exception("worker fatal error")
        await notify_admins(
            app,
            f"🛑 Worker is going down.\n{type(exc).__name__}: {exc}",
            dedupe_key="worker-fatal",
        )
        raise
    finally:
        dispatcher.cancel()
        await stop_log_drainer()
        health.close()
        await app.stop()
        await dispose_engine()


def main() -> None:
    asyncio.run(run())
