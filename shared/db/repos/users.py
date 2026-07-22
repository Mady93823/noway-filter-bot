"""User bookkeeping - one row per Telegram user, race-free upsert.

Postgres is the authority on who is banned; the bot reads a Redis
mirror on the hot path (bot/guards.py) so a ban check never costs a
database round trip per message.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import User


async def upsert_user(session: AsyncSession, user_id: int) -> None:
    await session.execute(
        pg_insert(User).values(id=user_id).on_conflict_do_nothing(index_elements=["id"])
    )


async def set_banned(
    session: AsyncSession, user_id: int, banned: bool, reason: str | None = None
) -> None:
    """Ban or unban. Inserts the row when the user never used the bot -
    an admin must be able to ban someone pre-emptively."""
    values = {
        "is_banned": banned,
        "banned_at": func.now() if banned else None,
        "ban_reason": reason if banned else None,
    }
    stmt = pg_insert(User).values(id=user_id, **values)
    await session.execute(
        stmt.on_conflict_do_update(index_elements=["id"], set_=values)
    )


async def banned_users(session: AsyncSession) -> list[User]:
    return list(
        (
            await session.scalars(
                select(User).where(User.is_banned.is_(True)).order_by(User.banned_at)
            )
        ).all()
    )


async def banned_ids(session: AsyncSession) -> list[int]:
    """Just the ids - what the Redis mirror is rebuilt from."""
    return list(
        (await session.scalars(select(User.id).where(User.is_banned.is_(True)))).all()
    )


async def user_exists(session: AsyncSession, user_id: int) -> bool:
    """Whether we have seen this user before - drives the new-user log."""
    return (
        await session.scalar(select(User.id).where(User.id == user_id))
    ) is not None


async def get_access_until(session: AsyncSession, user_id: int) -> datetime | None:
    return await session.scalar(select(User.access_until).where(User.id == user_id))


async def grant_access(
    session: AsyncSession, user_id: int, duration: timedelta
) -> datetime:
    """Extend a user's access window and return the new expiry.

    Extends rather than overwrites: someone with three weeks of premium
    left who completes a shortlink must not be cut back to four hours.
    The new expiry is (whichever is later of now and the current expiry)
    plus the duration.

    Inserts the row when the user has never messaged the bot, so an
    admin can grant premium to an id before its owner shows up.
    """
    now = datetime.now(timezone.utc)
    current = await get_access_until(session, user_id)
    base = current if current is not None and current > now else now
    expiry = base + duration

    stmt = pg_insert(User).values(id=user_id, access_until=expiry)
    await session.execute(
        stmt.on_conflict_do_update(index_elements=["id"], set_={"access_until": expiry})
    )
    return expiry


async def revoke_access(session: AsyncSession, user_id: int) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(access_until=None)
    )


async def active_access_count(session: AsyncSession) -> int:
    """How many users currently hold access - a /stats line worth having."""
    return (
        await session.scalar(
            select(func.count(User.id)).where(User.access_until > func.now())
        )
        or 0
    )


async def unban_all(session: AsyncSession) -> int:
    result = await session.execute(
        update(User)
        .where(User.is_banned.is_(True))
        .values(is_banned=False, banned_at=None, ban_reason=None)
    )
    return result.rowcount or 0
