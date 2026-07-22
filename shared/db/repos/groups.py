"""Group bookkeeping - one row per chat the bot has been added to.

Rows are never deleted when the bot is removed, only deactivated: the
group's filters hang off this row, and a kick is often accidental or
temporary. Re-adding the bot flips is_active back and the setup is
still there.
"""

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Group


async def upsert_group(
    session: AsyncSession, group_id: int, title: str | None = None
) -> None:
    """Record a group, or refresh a known one. Always marks it active.

    Called both when the bot is added and lazily on group activity, so
    groups that predate this bookkeeping get picked up without anyone
    having to re-add the bot.
    """
    values: dict[str, object] = {"is_active": True}
    if title is not None:
        values["title"] = title
    stmt = pg_insert(Group).values(id=group_id, title=title, is_active=True)
    await session.execute(
        stmt.on_conflict_do_update(index_elements=["id"], set_=values)
    )


async def set_active(session: AsyncSession, group_id: int, active: bool) -> None:
    await session.execute(
        update(Group).where(Group.id == group_id).values(is_active=active)
    )


async def active_groups(session: AsyncSession) -> list[Group]:
    return list(
        (
            await session.scalars(
                select(Group).where(Group.is_active.is_(True)).order_by(Group.added_at)
            )
        ).all()
    )


async def group_counts(session: AsyncSession) -> dict[str, int]:
    total = await session.scalar(select(func.count(Group.id)))
    active = await session.scalar(
        select(func.count(Group.id)).where(Group.is_active.is_(True))
    )
    return {"total": total or 0, "active": active or 0}
