"""Per-user engagement flourishes for funmode: search streaks + download
milestones.

State lives in Redis (golden rule 4 - never a module dict), so a streak
survives restarts and is the same whichever instance serves the user. Both
entry points are best-effort: a Redis hiccup returns None (no flourish) and
never breaks the search or the delivery it decorates. Counting itself is
gated on funmode by the caller, so a bot with the plain voice on writes
nothing here at all.

Dates are UTC calendar days - a "day streak" means consecutive UTC dates a
user searched, which is stable regardless of where the user or the server
sits.
"""

import logging
from datetime import datetime, timedelta, timezone

from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

_STREAK_COUNT = "streak:count:{}"
_STREAK_DAY = "streak:day:{}"
_DL_COUNT = "dl:count:{}"
_DL_DAY = "dl:day:{}"

# Download-count milestones. Keyed on the exact count, so each fires once.
_MILESTONES = {
    1: "🎬 first download! welcome to the club",
    10: "🔥 10 downloads — you're a regular now",
    25: "😎 25 files deep",
    50: "🍿 50 downloads. certified binger",
    100: "💯 CENTURY! certified cinephile 🏆",
    250: "🚀 250?? absolute unit",
    500: "🌟 500 downloads — hall of fame",
    1000: "👑 1000. you ARE the cinema now",
}


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _yesterday() -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


def milestone_line(count: int) -> str | None:
    """The line for a download count, or None. Pure - unit-testable."""
    return _MILESTONES.get(count)


async def touch_search(user_id: int) -> str | None:
    """Advance the user's daily search streak; return a hype line when it
    grew to two-plus days, else None (first day of a streak brags nothing).

    Idempotent within a day: the second search on the same date changes
    nothing and returns None, so the streak line shows at most once per day.
    """
    try:
        redis = get_redis()
        today = _today()
        last = await redis.get(_STREAK_DAY.format(user_id))
        if last == today:
            return None
        current = int(await redis.get(_STREAK_COUNT.format(user_id)) or 0)
        current = current + 1 if last == _yesterday() else 1
        await redis.set(_STREAK_COUNT.format(user_id), current)
        await redis.set(_STREAK_DAY.format(user_id), today)
        return f"🔥 {current} day streak" if current >= 2 else None
    except Exception as exc:
        logger.warning("streak update failed for %s: %s", user_id, exc)
        return None


async def touch_download(user_id: int) -> str | None:
    """Count one download; return the strongest flourish it earned.

    A milestone (exact 1/10/25/… count) wins; otherwise the first download
    of the UTC day gets a "first W of the day" nod; otherwise None.
    """
    try:
        redis = get_redis()
        count = await redis.incr(_DL_COUNT.format(user_id))
        line = milestone_line(count)
        today = _today()
        first_today = (await redis.get(_DL_DAY.format(user_id))) != today
        await redis.set(_DL_DAY.format(user_id), today)
        if line:
            return line
        return "☀️ gm — first W of the day" if first_today else None
    except Exception as exc:
        logger.warning("download milestone failed for %s: %s", user_id, exc)
        return None
