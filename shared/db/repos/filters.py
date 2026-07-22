"""Group keyword filters - ONE table for every group.

VJ-FILTER-BOT gave each group its own collection; here group_id is just
an indexed column, and (group_id, keyword) carries a unique constraint
so re-adding a keyword updates in place instead of racing.

reply is JSONB rather than a text column so the shape can grow (buttons,
media) without another migration. v1 stores {"text": "..."}.
"""

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Filter


async def add_filter(
    session: AsyncSession, group_id: int, keyword: str, text: str
) -> None:
    stmt = pg_insert(Filter).values(
        group_id=group_id, keyword=keyword, reply={"text": text}
    )
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_filters_group_keyword", set_={"reply": stmt.excluded.reply}
        )
    )


async def delete_filter(session: AsyncSession, group_id: int, keyword: str) -> bool:
    result = await session.execute(
        delete(Filter).where(Filter.group_id == group_id, Filter.keyword == keyword)
    )
    return bool(result.rowcount)


async def delete_all_filters(session: AsyncSession, group_id: int) -> int:
    result = await session.execute(delete(Filter).where(Filter.group_id == group_id))
    return result.rowcount or 0


async def filters_for_group(session: AsyncSession, group_id: int) -> dict[str, str]:
    """keyword -> reply text. The whole group's set in one query, because
    the caller caches it and matches in memory per message."""
    rows = await session.execute(
        select(Filter.keyword, Filter.reply)
        .where(Filter.group_id == group_id)
        .order_by(Filter.keyword)
    )
    return {keyword: (reply or {}).get("text", "") for keyword, reply in rows}


async def filter_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count(Filter.id))) or 0
