"""File-variant inserts.

Dedup happens HERE, atomically, via the unique constraint on
telegram_file_uid (ON CONFLICT DO NOTHING) - never a find-then-insert
scan. Same title with a different size/quality is a different row by
design (golden rule 6).
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import File


async def insert_file(
    session: AsyncSession,
    *,
    title_id: int,
    telegram_file_uid: str,
    telegram_file_id: str,
    raw_file_name: str | None,
    caption: str | None,
    quality: str | None,
    file_size: int | None,
    mime_type: str | None,
    source_channel_id: int,
    source_message_id: int,
    languages: list[str] | None = None,
    episodes: str | None = None,
) -> bool:
    """Insert one file variant. Returns False if it was already indexed."""
    stmt = (
        pg_insert(File)
        .values(
            title_id=title_id,
            telegram_file_uid=telegram_file_uid,
            telegram_file_id=telegram_file_id,
            raw_file_name=raw_file_name,
            caption=caption,
            quality=quality,
            episodes=episodes,
            file_size=file_size,
            mime_type=mime_type,
            source_channel_id=source_channel_id,
            source_message_id=source_message_id,
            languages=list(languages or []),
        )
        .on_conflict_do_nothing(index_elements=["telegram_file_uid"])
    )
    result = await session.execute(stmt)
    return bool(result.rowcount)


async def get_file(session: AsyncSession, file_db_id: int) -> File | None:
    return await session.get(File, file_db_id)


async def files_for_titles(
    session: AsyncSession, title_ids: list[int]
) -> dict[int, list[File]]:
    """All variants for the given titles: episode order, then smallest first.

    One movie is shown once with its variants listed (docs.md section 8) -
    this is the grouped fetch behind that display. For a series the
    episode label leads, so a season reads E01-E04 then E05-E08 rather
    than jumping around by file size.
    """
    if not title_ids:
        return {}
    rows = (
        await session.scalars(
            select(File)
            .where(File.title_id.in_(title_ids))
            .order_by(
                File.episodes.asc().nulls_first(),
                File.file_size.asc().nulls_last(),
                File.id,
            )
        )
    ).all()
    grouped: dict[int, list[File]] = {}
    for file in rows:
        grouped.setdefault(file.title_id, []).append(file)
    return grouped
