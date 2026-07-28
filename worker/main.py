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
from shared import reparse_state
from shared.telegram.client import create_client
from worker import enrich
from worker.backfill import run_backfill
from worker.live import register_live_handlers
from worker.reparse import reparse

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


async def _run_reparse(client: Client, job: dict) -> None:
    """Run one claimed re-parse, reporting every batch to Redis."""
    dry_run = job["dry_run"]
    logger.info("reparse starting (dry_run=%s)", dry_run)

    async def on_progress(
        phase: str, done: int, total: int, stats: dict[str, int]
    ) -> None:
        # Merged, not publish(total=total, **stats): stats carries its own
        # "total" key, and passing both is a TypeError that silently costs
        # the entire progress display while the run itself carries on.
        payload = dict(stats)
        payload.update(phase=phase, done=done, total=total)
        try:
            await reparse_state.publish(**payload)
        except Exception as exc:
            # Losing the progress display must never abort the re-parse
            # itself - the work is the point, the percentage is not.
            logger.warning("reparse progress publish failed: %s", exc)

    try:
        stats = await reparse(
            dry_run=dry_run,
            on_progress=on_progress,
            should_cancel=reparse_state.is_cancelled,
        )
    except Exception as exc:
        logger.exception("reparse failed")
        await reparse_state.finish(
            reparse_state.FAILED, error=f"{type(exc).__name__}: {exc}"
        )
        await notify_admins(
            client,
            f"💥 Re-parse failed.\n{type(exc).__name__}: {exc}",
            dedupe_key="reparse",
        )
        return

    cancelled = bool(stats.get("cancelled"))
    await reparse_state.finish(
        reparse_state.CANCELLED if cancelled else reparse_state.DONE, stats
    )
    prefix = "DRY RUN — " if dry_run else ""
    headline = "🛑 Re-parse stopped" if cancelled else "✅ Re-parse finished"
    await notify_admins(
        client,
        f"{prefix}{headline}\n"
        f"Files re-parsed: {stats['files']:,} of {stats['total']:,}\n"
        f"Moved to another title: {stats['moved']:,}\n"
        f"Display names repaired: {stats['renamed']:,}\n"
        f"Titles: {stats['titles_before']:,} → {stats['titles_after']:,} "
        f"({stats['orphans']:,} empty ones removed)",
    )


async def _reconcile_reparse() -> None:
    """A run still marked RUNNING at startup died with the last worker.

    Nothing else would ever clear it - the status lives in Redis, so it
    outlives the process that owned it, and a stale RUNNING makes every
    future /reparse refuse to start. The committed batches stay committed;
    re-queuing simply re-parses rows that are already correct, which costs
    time and changes nothing.
    """
    state = await reparse_state.read()
    if state["status"] != reparse_state.RUNNING:
        return
    logger.warning("clearing re-parse left RUNNING by a previous worker")
    # No stats argument: publish only writes the fields it is given, so the
    # counters the run had reached stay visible in /stats.
    await reparse_state.finish(
        reparse_state.FAILED,
        error="worker restarted mid-run - send /reparse again to finish it",
    )


async def reparse_dispatcher(client: Client) -> None:
    """Poll Redis for a queued re-parse and run at most one at a time.

    A re-parse rewrites every file row, so it runs here and not in the
    bot: the bot only writes the request and reads the progress. Awaiting
    the run inline is what keeps it to one at a time - the poll cannot
    come round again while a job is still going.
    """
    interval = get_settings().job_poll_interval
    while True:
        try:
            job = await reparse_state.claim()
            if job is not None:
                await _run_reparse(client, job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("reparse dispatcher iteration failed")
            await notify_admins(
                client,
                "⚠️ Worker re-parse dispatcher error.\n"
                f"{type(exc).__name__}: {exc}",
                dedupe_key="reparse-dispatcher",
            )
        await asyncio.sleep(interval)


async def enrich_dispatcher() -> None:
    """Background TMDB poster fetch for newly-resolved titles.

    Decoupled from the index coroutine on purpose: the API call happens
    here, never inside index_message, so the indexing/search path stays
    free of any live third-party call (golden rule 1). Idles at a longer
    interval when there is nothing pending, so a caught-up worker barely
    touches TMDB.
    """
    settings = get_settings()
    if not settings.enrich_enabled:
        logger.info("TMDB enrichment disabled (no TMDB credential) - not starting")
        return
    idle_interval = max(settings.enrich_interval, 60)
    async with enrich.make_client(settings) as http:
        while True:
            try:
                processed = await enrich.run_batch(http, settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Enrichment is best-effort cosmetics; a failure must never
                # take the worker down or spam admins. Log, back off, retry.
                logger.warning("enrich batch failed: %s", exc)
                processed = 0
            await asyncio.sleep(
                settings.enrich_interval if processed else idle_interval
            )


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
    await _reconcile_reparse()
    dispatcher = asyncio.create_task(job_dispatcher(app))
    reparser = asyncio.create_task(reparse_dispatcher(app))
    enricher = asyncio.create_task(enrich_dispatcher())
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
        reparser.cancel()
        enricher.cancel()
        await stop_log_drainer()
        health.close()
        await app.stop()
        await dispose_engine()


def main() -> None:
    asyncio.run(run())
