"""Proactive outbound rate governor (golden rule 8).

Telegram enforces its flood limits per BOT TOKEN, and this bot runs as
two processes - `bot` and `worker` - sharing one token. So the buckets
live in Redis, drawn from by both, never a per-process dict that would
let the two halves each spend the full budget.

Two token buckets gate every send:

  * global   - ~25 messages/second across all chats (Telegram's ceiling
    is ~30; we stay under it deliberately).
  * per-chat - ~1 message/second to any single chat. This is the limit
    that bites the log channel and any one busy group first, long before
    the global one does.

`acquire(chat_id)` blocks cooperatively until BOTH buckets allow the
send. Because Pyrogram routes `Message.reply_*` through the client's own
`send_*` methods, wrapping those methods once (`install_governor`) sends
every outbound message - deliveries, replies, logs, edits, deletes -
through here, with no change at the call sites. FloodWait stays as the
reactive backstop; the point of this module is to stop provoking it.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

_GLOBAL_KEY = "rl:global"
_CHAT_PREFIX = "rl:chat:"

# One script, both buckets, evaluated atomically. Checking them in two
# separate round trips could take a token from the global bucket and
# then block on the per-chat one, leaking the global token every retry.
# Here a send consumes from both only when both can pay; otherwise the
# refilled state is persisted and nothing is spent.
_TAKE_LUA = """
local function refill(key, rate, cap, now)
    local st = redis.call('HMGET', key, 't', 'ts')
    local tokens = tonumber(st[1])
    local ts = tonumber(st[2])
    if tokens == nil then return cap end
    local elapsed = now - ts
    if elapsed < 0 then elapsed = 0 end
    tokens = tokens + elapsed * rate / 1000.0
    if tokens > cap then tokens = cap end
    return tokens
end

local grate, gcap = tonumber(ARGV[1]), tonumber(ARGV[2])
local crate, ccap = tonumber(ARGV[3]), tonumber(ARGV[4])
local now = tonumber(ARGV[5])
local has_chat = tonumber(ARGV[6])

local gtok = refill(KEYS[1], grate, gcap, now)
local ctok = ccap
if has_chat == 1 then ctok = refill(KEYS[2], crate, ccap, now) end

local gwait = 0
if gtok < 1 then gwait = math.ceil((1 - gtok) * 1000.0 / grate) end
local cwait = 0
if has_chat == 1 and ctok < 1 then cwait = math.ceil((1 - ctok) * 1000.0 / crate) end
local wait = math.max(gwait, cwait)

if wait == 0 then
    redis.call('HSET', KEYS[1], 't', gtok - 1, 'ts', now)
    redis.call('PEXPIRE', KEYS[1], 10000)
    if has_chat == 1 then
        redis.call('HSET', KEYS[2], 't', ctok - 1, 'ts', now)
        redis.call('PEXPIRE', KEYS[2], 10000)
    end
    return 0
end

-- Blocked: persist the refilled tokens (unspent) with the new clock so
-- the caller's retry after `wait` ms sees the accrued credit.
redis.call('HSET', KEYS[1], 't', gtok, 'ts', now)
redis.call('PEXPIRE', KEYS[1], 10000)
if has_chat == 1 then
    redis.call('HSET', KEYS[2], 't', ctok, 'ts', now)
    redis.call('PEXPIRE', KEYS[2], 10000)
end
return wait
"""


class RateGovernor:
    def __init__(
        self,
        redis,
        *,
        global_rate: float = 25.0,
        global_capacity: float = 25.0,
        chat_rate: float = 1.0,
        chat_capacity: float = 1.0,
        max_wait: float = 30.0,
    ) -> None:
        self._redis = redis
        self._script = redis.register_script(_TAKE_LUA)
        self._grate = global_rate
        self._gcap = global_capacity
        self._crate = chat_rate
        self._ccap = chat_capacity
        # Never block a single send longer than this: past it, hand over
        # to FloodWait rather than pile coroutines up behind one chat.
        self._max_wait = max_wait

    async def _take(self, chat_id) -> int:
        has_chat = 1 if chat_id is not None else 0
        # A second key is always passed so numkeys stays 2; the script
        # only touches it when has_chat == 1.
        chat_key = f"{_CHAT_PREFIX}{chat_id}" if has_chat else f"{_CHAT_PREFIX}_"
        now = int(time.time() * 1000)
        wait = await self._script(
            keys=[_GLOBAL_KEY, chat_key],
            args=[self._grate, self._gcap, self._crate, self._ccap, now, has_chat],
        )
        return int(wait)

    async def acquire(self, chat_id=None) -> None:
        """Block until both buckets allow a send to chat_id (or globally)."""
        waited = 0.0
        while True:
            try:
                wait_ms = await self._take(chat_id)
            except Exception as exc:
                # Redis blip: fail OPEN. A governor that stops the bot
                # sending is worse than one that briefly lets it run
                # ungoverned - FloodWait still guards the real limit.
                logger.warning("rate governor unavailable, sending ungoverned: %s", exc)
                return
            if wait_ms <= 0:
                return
            sleep_s = wait_ms / 1000.0 + random.uniform(0.0, 0.05)
            waited += sleep_s
            if waited > self._max_wait:
                logger.warning(
                    "rate governor waited %.1fs for chat %s - proceeding",
                    waited,
                    chat_id,
                )
                return
            await asyncio.sleep(sleep_s)


# The methods every outbound message ultimately flows through. Wrapping
# these on the client instance also catches Message.reply_* and
# CallbackQuery.edit_*, which delegate to them. Callback ANSWERS are not
# here on purpose: they are not chat messages and have far looser limits.
_SEND_METHODS = (
    "send_message",
    "send_cached_media",
    "edit_message_text",
    "delete_messages",
)


def _wrap(governor: RateGovernor, original):
    async def governed(*args, **kwargs):
        chat_id = kwargs.get("chat_id")
        if chat_id is None and args:
            chat_id = args[0]
        await governor.acquire(chat_id)
        return await original(*args, **kwargs)

    return governed


def install_governor(client, governor: RateGovernor | None = None) -> RateGovernor:
    """Route every outbound send on `client` through the rate governor.

    Call once per client, right after it is created and before it starts
    serving.
    """
    gov = governor or RateGovernor(get_redis())
    for name in _SEND_METHODS:
        original = getattr(client, name, None)
        if original is None:
            continue
        setattr(client, name, _wrap(gov, original))
    return gov
