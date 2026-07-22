"""Ban enforcement.

Postgres owns the truth (users.is_banned); Redis holds a mirror so the
check on every incoming message is one SISMEMBER instead of a database
round trip. Golden rule 4: the mirror lives in Redis, never in a
module-level set, so every instance sees a ban the moment it is issued.

A Redis SET with no members does not exist, so "nobody is banned" and
"the mirror was never built" look identical. A separate marker key tells
them apart, and a missing marker rebuilds the mirror from Postgres - the
guard heals itself after a Redis flush instead of silently letting every
banned user back in.
"""

import logging

from pyrogram import filters
from pyrogram.types import Message

from shared.db.engine import get_session_factory
from shared.db.repos import users as users_repo
from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

_BANS_KEY = "bans"
_LOADED_KEY = "bans:loaded"


async def refresh_bans() -> int:
    """Rebuild the Redis mirror from Postgres. Returns the ban count."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        ids = await users_repo.banned_ids(session)

    redis = get_redis()
    pipe = redis.pipeline()
    pipe.delete(_BANS_KEY)
    if ids:
        pipe.sadd(_BANS_KEY, *[str(user_id) for user_id in ids])
    pipe.set(_LOADED_KEY, "1")
    await pipe.execute()
    logger.info("ban mirror rebuilt: %d banned", len(ids))
    return len(ids)


async def sync_ban(user_id: int, banned: bool) -> None:
    """Keep the mirror in step with a just-committed DB change."""
    redis = get_redis()
    if banned:
        await redis.sadd(_BANS_KEY, str(user_id))
    else:
        await redis.srem(_BANS_KEY, str(user_id))


async def is_banned(user_id: int | None) -> bool:
    if user_id is None:
        return False
    redis = get_redis()
    if not await redis.exists(_LOADED_KEY):
        await refresh_bans()
    return bool(await redis.sismember(_BANS_KEY, str(user_id)))


async def _not_banned(_, __, message: Message) -> bool:
    # async on purpose: a sync predicate makes Pyrogram dispatch the whole
    # filter chain through client.loop.run_in_executor.
    user = getattr(message, "from_user", None)
    return not await is_banned(user.id if user else None)


# Banned users get silence, not an error. Naming the command that tripped
# the ban only tells them what to work around, and any reply is
# engagement - exactly what a spammer is after.
not_banned = filters.create(_not_banned)
