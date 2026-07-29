"""Per-user ownership of group result cards.

In a group everyone sees everyone's result card, and its buttons are
public - so without this a second user can page, filter, or close a card
the first user opened, which is exactly the annoyance this guards against.

The owner is remembered in Redis (golden rule 4: cross-request state never
lives in a module dict, so every instance agrees on who owns a card) keyed
by the card's message, with the same TTL the card self-deletes on - the
record never outlives the thing it protects. Private chats are never
tracked: a PM has exactly one user, so there is nothing to own.

Fail-open on purpose. A missing record (Redis flushed, a card sent before
this shipped, a lookup error) reads as "no known owner" and the tap is
allowed. Locking the real owner out of their own card over a Redis hiccup
would be worse than letting the occasional orphaned card stay public.
"""

import logging

from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

_PREFIX = "cardowner:"


def _key(chat_id: int, message_id: int) -> str:
    return f"{_PREFIX}{chat_id}:{message_id}"


async def remember(chat_id: int, message_id: int, user_id: int, ttl: int) -> None:
    """Record who owns a group card. Never raises - a failed write just
    means that card falls back to being public (fail-open)."""
    try:
        await get_redis().set(_key(chat_id, message_id), str(user_id), ex=max(1, ttl))
    except Exception as exc:
        logger.warning("could not record card owner for %s: %s", message_id, exc)


async def owner_of(chat_id: int, message_id: int) -> int | None:
    """The user id that owns this card, or None when unknown (fail-open)."""
    try:
        raw = await get_redis().get(_key(chat_id, message_id))
    except Exception as exc:
        logger.warning("card owner lookup failed for %s: %s", message_id, exc)
        return None
    return int(raw) if raw and raw.lstrip("-").isdigit() else None
