"""Self-deleting group messages.

Groups are the main surface of this bot, and a busy one fills with
result cards within minutes. Anything the bot says in a group is
therefore temporary: it is scheduled for deletion when it is sent, and a
sweeper removes it once due.

The schedule lives in a Redis sorted set, not in asyncio tasks. A
`create_task(sleep(300))` per message looks simpler and is wrong here
for three reasons, all of which bite in production:

    * the task dies with the process, so every restart leaks whatever
      was pending - those messages then live forever
    * thousands of sleeping tasks means thousands of live coroutines
    * with two instances running, neither knows what the other owes

A sorted set scored by deletion time makes the whole thing one range
query per tick, survives restarts, and lets any instance do the work
(golden rule 4 - cross-request state belongs in Redis).

PM is never touched. A user's own chat is their archive; deleting the
file they came for would be hostile.
"""

import asyncio
import logging
import time

from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

_KEY = "ephemeral"
# One pass every 20s: the visible error on a 5 minute lifetime is at
# most 20 seconds, and the cost is a single ZRANGEBYSCORE per tick.
_TICK = 20
# Bounded per pass so one enormous backlog cannot stall the loop.
_BATCH = 200


async def schedule_delete(chat_id: int, message_id: int, ttl: int) -> None:
    """Mark one message for deletion in ttl seconds. Never raises."""
    try:
        await get_redis().zadd(_KEY, {f"{chat_id}:{message_id}": time.time() + ttl})
    except Exception as exc:
        # A message that fails to schedule simply stays - far better than
        # failing the reply the user is waiting on.
        logger.warning("could not schedule deletion for %s: %s", message_id, exc)


async def expire_in_group(source, sent) -> None:
    """Schedule a bot reply for deletion, but only when it is in a group.

    Takes the incoming message as well as the sent one so every caller
    makes the same PM-vs-group decision in the same place rather than
    each re-deriving it.
    """
    from pyrogram.enums import ChatType

    from shared.config import get_settings

    if sent is None or source.chat.type == ChatType.PRIVATE:
        return
    await schedule_delete(
        sent.chat.id, sent.id, get_settings().group_message_ttl
    )


async def sweep_once(client) -> int:
    """Delete everything now due. Returns how many were actually removed."""
    redis = get_redis()
    due = await redis.zrangebyscore(_KEY, 0, time.time(), start=0, num=_BATCH)
    if not due:
        return 0

    removed = 0
    for member in due:
        # rpartition, not split: chat ids are negative and message ids
        # are the tail, so splitting on the first ":" would be wrong.
        chat_part, _, message_part = member.rpartition(":")
        try:
            await client.delete_messages(int(chat_part), int(message_part))
            removed += 1
        except Exception as exc:
            # Already deleted by a user, or the bot lost its rights.
            # Either way the entry has done its job.
            logger.debug("ephemeral delete failed for %s: %s", member, exc)

    # Dropped whether or not the delete succeeded, so a permanently
    # undeletable message cannot wedge the queue forever.
    await redis.zrem(_KEY, *due)
    return removed


async def sweeper(client) -> None:
    """Background loop. Cancelled on shutdown by the caller."""
    logger.info("ephemeral sweeper started")
    while True:
        try:
            await sweep_once(client)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The loop outliving a bad tick matters more than the tick.
            logger.exception("ephemeral sweep failed")
        await asyncio.sleep(_TICK)
