"""Demand tracking for titles the index does not have.

A single failed search means nothing - people mistype, and people ask
for films that were never released. Three different people asking for
the same thing inside a day is a signal worth acting on, and that is
what reaches the log channel.

Distinct users, not hits: counting raw misses would let one person
hammering the same typo manufacture a request. A Redis SET keyed on the
query hash does that deduplication for free, and its TTL provides the
24h window with no sweeper job.

A second marker key stops the alert repeating once the threshold is
crossed - otherwise every further miss on a popular title would post
again for the rest of the day.
"""

import logging

from shared.redis_client import get_redis
from shared.search.cache import query_hash

logger = logging.getLogger(__name__)

_MISS_PREFIX = "miss:"
_DONE_PREFIX = "miss:done:"
_WINDOW = 86_400  # 24h, matching "asked repeatedly within a day"


async def record_miss(query: str, user_id: int, threshold: int) -> int | None:
    """Count one failed search. Returns the count when it first crosses.

    None means "not newsworthy" - either below the threshold, or already
    reported during this window. The caller logs only on a number.
    """
    try:
        redis = get_redis()
        digest = query_hash(query)
        key = _MISS_PREFIX + digest
        done = _DONE_PREFIX + digest

        if await redis.exists(done):
            return None

        added = await redis.sadd(key, str(user_id))
        # Refresh the TTL on every miss, so a title asked for daily keeps
        # its window open instead of expiring mid-conversation.
        await redis.expire(key, _WINDOW)
        if not added:
            return None  # this user already asked; not a new voice

        count = await redis.scard(key)
        if count < threshold:
            return None

        # Claim the report; if another instance got there first, stay quiet.
        if not await redis.set(done, "1", ex=_WINDOW, nx=True):
            return None
        return count
    except Exception as exc:
        # Demand tracking is a nicety - it must never break a search.
        logger.warning("demand tracking failed for %r: %s", query, exc)
        return None
