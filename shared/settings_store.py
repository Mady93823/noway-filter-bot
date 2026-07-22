"""Runtime settings an admin edits from chat (bot_settings table).

Two config systems exist on purpose and they are not interchangeable:

    shared/config.py    deployment facts, read from the environment once
                        at process start - tokens, database URLs, admin
                        ids. Changing one means a redeploy.
    this module         things an admin changes while the bot is running
                        - log channel, shortener credentials, whether the
                        gate is on, how long a verification lasts.

Postgres owns the value; Redis caches it briefly. Both matter: the bot
and the worker are separate processes, so /setlog in one has to become
visible in the other without a restart (golden rule 4 - no module-level
dict), and a settings read sits in the delivery path, where a database
round trip per file tap is wasteful. Writes invalidate the cache
immediately, so the TTL only bounds staleness for a value changed on
another instance.
"""

import logging

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from shared.db.engine import get_session_factory
from shared.db.models import BotSetting
from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "cfg:"
_CACHE_TTL = 60
# Sentinel for "there is no such setting". Without it a missing key would
# be re-queried on every single call, which is the exact hot-path cost
# the cache exists to avoid.
_MISSING = "\x00"

LOG_CHANNEL = "log_channel_id"
SHORTENER_API = "shortener_api"
SHORTENER_BASE = "shortener_base"
GATE_ENABLED = "gate_enabled"
ACCESS_HOURS = "access_hours"

DEFAULT_SHORTENER_BASE = "https://arolinks.com/api"
DEFAULT_ACCESS_HOURS = 4


async def get_setting(key: str) -> str | None:
    redis = get_redis()
    cached = await redis.get(_CACHE_PREFIX + key)
    if cached is not None:
        return None if cached == _MISSING else cached

    session_factory = get_session_factory()
    async with session_factory() as session:
        value = await session.scalar(
            select(BotSetting.value).where(BotSetting.key == key)
        )
    await redis.set(
        _CACHE_PREFIX + key, _MISSING if value is None else value, ex=_CACHE_TTL
    )
    return value


async def set_setting(key: str, value: str) -> None:
    session_factory = get_session_factory()
    async with session_factory() as session, session.begin():
        await session.execute(
            pg_insert(BotSetting)
            .values(key=key, value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        )
    # Invalidate rather than overwrite: the write is what just happened,
    # so the next read should come from the row that actually landed.
    await get_redis().delete(_CACHE_PREFIX + key)


async def clear_setting(key: str) -> None:
    session_factory = get_session_factory()
    async with session_factory() as session, session.begin():
        await session.execute(delete(BotSetting).where(BotSetting.key == key))
    await get_redis().delete(_CACHE_PREFIX + key)


async def log_channel_id() -> int | None:
    """Channel that receives event logs, or None when unset."""
    raw = await get_setting(LOG_CHANNEL)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        # Someone stored junk by hand; logging must not start crashing
        # because of it.
        logger.warning("log channel setting is not an integer: %r", raw)
        return None


async def gate_enabled() -> bool:
    return (await get_setting(GATE_ENABLED)) == "1"


async def access_hours() -> int:
    raw = await get_setting(ACCESS_HOURS)
    try:
        hours = int(raw) if raw is not None else DEFAULT_ACCESS_HOURS
    except ValueError:
        hours = DEFAULT_ACCESS_HOURS
    return max(1, hours)


async def shortener_config() -> tuple[str | None, str]:
    """(api_token, base_url). A None token means the gate cannot run."""
    token = await get_setting(SHORTENER_API)
    base = await get_setting(SHORTENER_BASE) or DEFAULT_SHORTENER_BASE
    return token, base


def mask_token(token: str) -> str:
    """Never echo a full API token back into a chat.

    /showconfig output can be forwarded or screenshotted; the last four
    characters are enough for an admin to tell which key is loaded.
    """
    if len(token) <= 4:
        return "…"
    return "…" + token[-4:]
