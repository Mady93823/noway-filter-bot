"""Poster rows - one per TMDB entity, shared by every title that maps to it.

Written only by the offline enricher (worker/enrich.py); read by search to
attach artwork to a result. Dedup is the whole point: upsert keys on tmdb_id
so a series' seasons and a film's size variants never store the URL twice.
"""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Poster


async def upsert_poster(
    session: AsyncSession,
    *,
    tmdb_id: int,
    media_type: str,
    poster_url: str | None,
    overview: str | None = None,
    vote: float | None = None,
) -> None:
    """Insert a poster, or refresh its fields if the tmdb_id already exists.

    Refresh (not DO NOTHING) so a later, better fetch can fill artwork that
    an earlier one missed - TMDB sometimes has the entity before the poster.
    fetched_at advances on every write so staleness is visible.
    """
    stmt = pg_insert(Poster).values(
        tmdb_id=tmdb_id,
        media_type=media_type,
        poster_url=poster_url,
        overview=overview,
        vote=vote,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Poster.tmdb_id],
        set_={
            "media_type": stmt.excluded.media_type,
            "poster_url": stmt.excluded.poster_url,
            "overview": stmt.excluded.overview,
            "vote": stmt.excluded.vote,
            "fetched_at": func.now(),
        },
    )
    await session.execute(stmt)


async def poster_urls_for(
    session: AsyncSession, tmdb_ids: list[int]
) -> dict[int, str | None]:
    """Map the given tmdb_ids to their stored poster URL (missing keys omitted)."""
    if not tmdb_ids:
        return {}
    rows = (
        await session.scalars(
            select(Poster).where(Poster.tmdb_id.in_(set(tmdb_ids)))
        )
    ).all()
    return {row.tmdb_id: row.poster_url for row in rows}
