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

The same queue runs delivered files, on a second sorted set. Those are in
PM, which this module otherwise never touches, so they get their own
treatment: the file is deleted, and the warning message sent with it is
rewritten in place to say so. A chat that silently loses a message is
worse than one that explains itself.
"""

import asyncio
import logging
import time

from pyrogram.enums import ParseMode

from bot import ui
from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

_KEY = "ephemeral"
# Delivered files. Member is "<chat_id>:<file_msg_id>:<notice_msg_id>";
# a notice id of 0 means the warning never sent and only the file is due.
_DELIVERY_KEY = "delivery-expiry"
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


async def schedule_delivery_expiry(
    chat_id: int, file_message_id: int, notice_message_id: int, ttl: int
) -> None:
    """Mark a delivered file (and its warning) for expiry. Never raises.

    Kept separate from schedule_delete because the two ends differ: this
    one deletes the file and REWRITES the notice, so both ids have to
    travel together through Redis rather than as two independent entries
    that could expire apart.
    """
    try:
        await get_redis().zadd(
            _DELIVERY_KEY,
            {
                f"{chat_id}:{file_message_id}:{notice_message_id}": time.time() + ttl
            },
        )
    except Exception as exc:
        # Same trade as schedule_delete: a file that fails to schedule
        # simply stays. Losing the delivery over it would be worse.
        logger.warning(
            "could not schedule expiry for %s in %s: %s",
            file_message_id,
            chat_id,
            exc,
        )


async def _sweep_deliveries(client) -> int:
    """Expire delivered files: delete the file, rewrite its notice."""
    redis = get_redis()
    due = await redis.zrangebyscore(
        _DELIVERY_KEY, 0, time.time(), start=0, num=_BATCH
    )
    if not due:
        return 0

    removed = 0
    for member in due:
        # Two rpartitions from the right: the chat id is negative in
        # groups and both message ids are the tail, so splitting from the
        # left would misread the sign.
        head, _, notice_part = member.rpartition(":")
        chat_part, _, file_part = head.rpartition(":")
        try:
            chat_id, file_id, notice_id = (
                int(chat_part), int(file_part), int(notice_part)
            )
        except ValueError:
            logger.debug("malformed delivery entry %s", member)
            continue
        try:
            await client.delete_messages(chat_id, file_id)
            removed += 1
        except Exception as exc:
            logger.debug("delivery delete failed for %s: %s", member, exc)
        if notice_id:
            try:
                await client.edit_message_text(
                    chat_id,
                    notice_id,
                    ui.delivery_expired_text(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as exc:
                # The user may have deleted the notice themselves. The
                # file is already gone either way.
                logger.debug("delivery notice rewrite failed for %s: %s", member, exc)

    await redis.zrem(_DELIVERY_KEY, *due)
    return removed


async def sweep_once(client) -> int:
    """Delete everything now due. Returns how many were actually removed."""
    return await _sweep_group(client) + await _sweep_deliveries(client)


async def _sweep_group(client) -> int:
    """Expire bot messages posted in groups."""
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
