"""index_progress access - the checkpoint table that makes indexing
resumable (golden rule 7). Every mutation lands in the DB immediately;
nothing about job state lives only in worker memory.
"""

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import IndexProgress, JobStatus


async def upsert_job(
    session: AsyncSession, channel_id: int, target_message_id: int
) -> None:
    """Create or restart a backfill job.

    Re-running /index on an already-indexed channel keeps the checkpoint
    (resume semantics) and only raises the target - it never rescans
    completed history.
    """
    stmt = pg_insert(IndexProgress).values(
        channel_id=channel_id,
        target_message_id=target_message_id,
        status=JobStatus.RUNNING,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["channel_id"],
        set_={
            "target_message_id": func.greatest(
                IndexProgress.target_message_id, stmt.excluded.target_message_id
            ),
            "status": JobStatus.RUNNING.value,
            "error": None,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)


async def get_job(session: AsyncSession, channel_id: int) -> IndexProgress | None:
    return await session.get(IndexProgress, channel_id)


async def list_running(session: AsyncSession) -> list[IndexProgress]:
    result = await session.scalars(
        select(IndexProgress).where(IndexProgress.status == JobStatus.RUNNING)
    )
    return list(result)


async def checkpoint(
    session: AsyncSession, channel_id: int, last_processed_message_id: int
) -> None:
    await session.execute(
        update(IndexProgress)
        .where(IndexProgress.channel_id == channel_id)
        .values(
            last_processed_message_id=last_processed_message_id,
            updated_at=func.now(),
        )
    )


async def set_status(
    session: AsyncSession,
    channel_id: int,
    status: JobStatus,
    error: str | None = None,
) -> None:
    await session.execute(
        update(IndexProgress)
        .where(IndexProgress.channel_id == channel_id)
        .values(status=status.value, error=error, updated_at=func.now())
    )
