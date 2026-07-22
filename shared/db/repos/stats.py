"""Read-only stats queries + the admin index wipe."""

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import File, IndexProgress, Title, User


async def index_counts(session: AsyncSession) -> dict[str, int]:
    titles = await session.scalar(select(func.count(Title.id)))
    files = await session.scalar(select(func.count(File.id)))
    users = await session.scalar(select(func.count(User.id)))
    return {"titles": titles or 0, "files": files or 0, "users": users or 0}


async def total_file_bytes(session: AsyncSession) -> int:
    return await session.scalar(select(func.coalesce(func.sum(File.file_size), 0)))


async def job_rows(session: AsyncSession) -> list[IndexProgress]:
    return list(
        (await session.scalars(select(IndexProgress).order_by(IndexProgress.channel_id))).all()
    )


async def db_size_bytes(session: AsyncSession) -> int:
    return await session.scalar(text("SELECT pg_database_size(current_database())"))


async def wipe_index(session: AsyncSession) -> dict[str, int]:
    """TRUNCATE all indexed data. Returns pre-wipe counts for the report.

    users/groups/filters survive - only titles (cascades to files) and
    index_progress go.
    """
    counts = await index_counts(session)
    await session.execute(text("TRUNCATE titles CASCADE"))
    await session.execute(text("TRUNCATE index_progress"))
    return counts
