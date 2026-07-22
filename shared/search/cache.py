"""Redis-backed search result cache and pagination cursors (golden rule 4).

The ordered title-id list for a query lives in Redis with a TTL - never
in process memory - so pagination survives restarts and scales across
instances. A cursor is "<qhash>:<offset>": tiny enough for Telegram
callback_data (64-byte limit). Deep pages slice the cached list; there
is no OFFSET query against Postgres.
"""

import hashlib
import json

from shared.config import get_settings
from shared.redis_client import get_redis

_KEY_PREFIX = "search:"
# Per user, per chat: the same person refining in a group and in PM are
# two conversations, and two people refining in one group must not
# overwrite each other's context.
_LAST_PREFIX = "lastq:"
_HASH_LENGTH = 12


def conversation_scope(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


async def store_last_query(scope: str, normalized_query: str) -> None:
    """Remember what this user last searched, so "1080p" can refine it.

    Redis, not a dict (golden rule 4): refinement must survive a restart
    and behave the same on every instance. Shares search_cache_ttl - once
    the result set behind it has expired, refining it would be answering
    from context the user can no longer see.
    """
    await get_redis().set(
        _LAST_PREFIX + scope, normalized_query, ex=get_settings().search_cache_ttl
    )


async def load_last_query(scope: str) -> str | None:
    return await get_redis().get(_LAST_PREFIX + scope)


def query_hash(normalized_query: str) -> str:
    return hashlib.sha1(normalized_query.encode("utf-8")).hexdigest()[:_HASH_LENGTH]


def encode_cursor(qhash: str, offset: int) -> str:
    return f"{qhash}:{offset}"


def decode_cursor(cursor: str) -> tuple[str, int] | None:
    qhash, sep, offset = cursor.partition(":")
    if not sep or len(qhash) != _HASH_LENGTH or not offset.isdigit():
        return None
    return qhash, int(offset)


async def store_results(qhash: str, normalized_query: str, title_ids: list[int]) -> None:
    payload = json.dumps({"q": normalized_query, "ids": title_ids})
    await get_redis().set(
        _KEY_PREFIX + qhash, payload, ex=get_settings().search_cache_ttl
    )


async def load_results(qhash: str) -> tuple[str, list[int]] | None:
    """Returns (normalized_query, title_ids) or None once the TTL expired."""
    raw = await get_redis().get(_KEY_PREFIX + qhash)
    if raw is None:
        return None
    data = json.loads(raw)
    return data["q"], list(data["ids"])
