"""Resumable channel-history backfill (docs.md section 6).

Bots cannot enumerate channel history, so the walker steps through
ascending message-id ranges with get_messages. The checkpoint row in
index_progress is updated after EVERY batch - a crash resumes from the
last checkpoint automatically, and re-touched boundary messages are
no-ops thanks to the telegram_file_uid unique constraint.
"""

import logging
import time

from pyrogram import Client
from pyrogram.errors import FloodWait, MessageIdsEmpty, RPCError

from shared.config import get_settings
from shared.db.engine import get_session_factory
from shared.db.models import JobStatus
from shared.db.repos import progress as progress_repo
from worker.indexer import IndexOutcome, index_message
from worker.pacing import AdaptivePacer

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_ERRORS = 5


class ProgressReporter:
    """DMs 'indexed x/x' to every admin at most once per interval.

    One message per admin, edited in place afterwards. Report failures
    (admin never opened the bot, FloodWait) are logged and skipped -
    reporting must never stall or kill the backfill itself.
    """

    def __init__(self, client: Client, channel_id: int, interval: float) -> None:
        self._client = client
        self._channel_id = channel_id
        self._interval = interval
        self._admin_message_ids: dict[int, int] = {}
        self._last_sent = 0.0

    async def _push(self, text: str) -> None:
        for admin_id in get_settings().admin_ids:
            try:
                message_id = self._admin_message_ids.get(admin_id)
                if message_id is None:
                    sent = await self._client.send_message(admin_id, text)
                    self._admin_message_ids[admin_id] = sent.id
                else:
                    await self._client.edit_message_text(admin_id, message_id, text)
            except Exception as exc:
                logger.warning("progress report to %s failed: %s", admin_id, exc)

    async def maybe_report(
        self, done: int, target: int, total: dict["IndexOutcome", int]
    ) -> None:
        if time.monotonic() - self._last_sent < self._interval:
            return
        self._last_sent = time.monotonic()
        percent = done / target * 100 if target else 0.0
        await self._push(
            f"⏳ Indexing {self._channel_id}\n"
            f"indexed {done:,}/{target:,} ({percent:.1f}%)\n"
            f"new {total[IndexOutcome.INDEXED]:,} · "
            f"already {total[IndexOutcome.DUPLICATE]:,} · "
            f"skipped {total[IndexOutcome.SKIPPED]:,}"
        )

    async def final(self, text: str) -> None:
        # Final notice always goes out as a FRESH message so it pings the
        # admin (edits are silent).
        self._admin_message_ids = {}
        await self._push(text)


async def run_backfill(client: Client, channel_id: int) -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    pacer = AdaptivePacer(settings.base_batch_delay)
    consecutive_errors = 0
    # Totals since this run started (a resume restarts them at zero -
    # the durable progress lives in index_progress, not here).
    total = {outcome: 0 for outcome in IndexOutcome}
    reporter = ProgressReporter(
        client, channel_id, float(settings.progress_report_interval)
    )

    while True:
        # Re-read the job each loop so an external pause takes effect.
        async with session_factory() as session:
            job = await progress_repo.get_job(session, channel_id)
        if job is None or job.status != JobStatus.RUNNING:
            return

        start = job.last_processed_message_id + 1
        if start > job.target_message_id:
            async with session_factory() as session, session.begin():
                await progress_repo.set_status(session, channel_id, JobStatus.COMPLETED)
            logger.info("backfill completed for channel %s", channel_id)
            await reporter.final(
                f"✅ Backfill complete for {channel_id}\n"
                f"indexed {job.target_message_id:,}/{job.target_message_id:,}\n"
                f"new {total[IndexOutcome.INDEXED]:,} · "
                f"already {total[IndexOutcome.DUPLICATE]:,} · "
                f"skipped {total[IndexOutcome.SKIPPED]:,}\n"
                "New posts in this channel are now indexed automatically."
            )
            return

        end = min(start + settings.backfill_batch_size - 1, job.target_message_id)
        try:
            messages = await client.get_messages(
                channel_id, list(range(start, end + 1))
            )
        except FloodWait as exc:
            logger.warning(
                "FloodWait %ss on channel %s - honoring and backing off",
                exc.value,
                channel_id,
            )
            await pacer.on_flood_wait(float(exc.value))
            continue
        except MessageIdsEmpty:
            # Telegram serves nothing for this id window - a span of
            # deleted messages, service-only ids, or a gap below the real
            # head. Skip it and advance the checkpoint; halting a 900k
            # backfill at 25% because one 100-id window is empty is the
            # worse failure. Re-touching these ids later stays a no-op, so
            # skipping loses nothing real.
            logger.warning(
                "empty id range %s-%s on channel %s - skipping",
                start,
                end,
                channel_id,
            )
            consecutive_errors = 0
            async with session_factory() as session, session.begin():
                await progress_repo.checkpoint(session, channel_id, end)
            await pacer.wait()
            continue
        except RPCError as exc:
            consecutive_errors += 1
            logger.error(
                "RPC error on channel %s batch %s-%s (attempt %s): %s",
                channel_id,
                start,
                end,
                consecutive_errors,
                exc,
            )
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                async with session_factory() as session, session.begin():
                    await progress_repo.set_status(
                        session, channel_id, JobStatus.ERRORED, error=str(exc)
                    )
                await reporter.final(
                    f"❌ Backfill for {channel_id} stopped after repeated "
                    f"errors: {exc}\nFix the cause (is the bot an admin in "
                    "that channel?) and forward a post again to resume."
                )
                return
            await pacer.on_error()
            continue

        consecutive_errors = 0
        batch = {outcome: 0 for outcome in IndexOutcome}
        for message in messages or []:
            if message is None or getattr(message, "empty", False):
                batch[IndexOutcome.SKIPPED] += 1
                continue
            try:
                outcome = await index_message(message)
            except Exception:
                logger.exception(
                    "failed to index %s/%s - continuing", channel_id, message.id
                )
                continue
            batch[outcome] += 1
        for outcome, count in batch.items():
            total[outcome] += count

        logger.info(
            "channel %s | batch %s-%s: new %s · already %s · skipped %s"
            " | total: new %s · already %s · skipped %s",
            channel_id,
            start,
            end,
            batch[IndexOutcome.INDEXED],
            batch[IndexOutcome.DUPLICATE],
            batch[IndexOutcome.SKIPPED],
            total[IndexOutcome.INDEXED],
            total[IndexOutcome.DUPLICATE],
            total[IndexOutcome.SKIPPED],
        )

        async with session_factory() as session, session.begin():
            await progress_repo.checkpoint(session, channel_id, end)
        await reporter.maybe_report(end, job.target_message_id, total)
        pacer.record_success()
        await pacer.wait()
