"""Single async Redis client factory.

Search-result caching, pagination cursors and any other cross-request
state (golden rule 4) must live here or in Postgres - never in
module-level Python dicts.
"""

import redis.asyncio as redis

from shared.config import get_settings

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(
            get_settings().redis_url,
            decode_responses=True,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
