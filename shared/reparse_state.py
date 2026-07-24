"""Re-parse job state, shared between the bot and the worker via Redis.

The bot asks for a re-parse and reports its progress; the worker is what
actually runs it (the bot never does indexing work). Neither process can
see the other's memory, so the entire job - request, phase, counters,
cancel flag, result - lives in one Redis hash (golden rule 4). A restart
of either side loses nothing but the current batch, and /stats shows the
same numbers no matter which process is asked.
"""

import time
from typing import Any

from shared.redis_client import get_redis

STATE_KEY = "reparse:state"

IDLE = "idle"
REQUESTED = "requested"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

# Queued or in flight: a second request must be refused, not stacked.
ACTIVE = (REQUESTED, RUNNING)

_COUNTERS = (
    "total",
    "done",
    "files",
    "moved",
    "renamed",
    "orphans",
    "titles_before",
    "titles_after",
)
_TIMES = ("requested_at", "started_at", "updated_at", "finished_at")

# Compare-and-set in Lua rather than HGET-then-HSET: two worker processes
# polling in the same second would both read REQUESTED and both start
# rewriting every file row in the index.
_CLAIM = """
if redis.call('HGET', KEYS[1], 'status') ~= 'requested' then return 0 end
redis.call('HSET', KEYS[1], 'status', 'running',
           'started_at', ARGV[1], 'updated_at', ARGV[1])
return 1
"""


def _encode(value: Any) -> str:
    if value is True:
        return "1"
    if value is False:
        return "0"
    return str(value)


async def read() -> dict[str, Any]:
    """Current state, with defaults so a never-run job reads cleanly."""
    raw = await get_redis().hgetall(STATE_KEY) or {}
    state: dict[str, Any] = {
        "status": raw.get("status", IDLE),
        "phase": raw.get("phase", ""),
        "dry_run": raw.get("dry_run") == "1",
        "cancel": raw.get("cancel") == "1",
        "error": raw.get("error", ""),
    }
    for name in _COUNTERS:
        state[name] = int(raw.get(name) or 0)
    for name in _TIMES:
        state[name] = float(raw.get(name) or 0)
    return state


async def request(dry_run: bool, by: int) -> bool:
    """Queue a run. False if one is already queued or in flight.

    The old hash is deleted first so the counters an admin sees belong to
    this run and not to the previous one.
    """
    redis = get_redis()
    if (await redis.hget(STATE_KEY, "status")) in ACTIVE:
        return False
    now = str(time.time())
    await redis.delete(STATE_KEY)
    await redis.hset(
        STATE_KEY,
        mapping={
            "status": REQUESTED,
            "dry_run": "1" if dry_run else "0",
            "by": str(by),
            "requested_at": now,
            "updated_at": now,
        },
    )
    return True


async def claim() -> dict[str, Any] | None:
    """Worker side: take a queued run, exactly once. None if nothing queued."""
    if not await get_redis().eval(_CLAIM, 1, STATE_KEY, str(time.time())):
        return None
    return await read()


async def publish(**fields: Any) -> None:
    """Write progress. Cheap enough to call once per batch."""
    mapping = {name: _encode(value) for name, value in fields.items()}
    mapping["updated_at"] = str(time.time())
    await get_redis().hset(STATE_KEY, mapping=mapping)


async def finish(
    status: str, stats: dict[str, int] | None = None, error: str = ""
) -> None:
    fields: dict[str, Any] = dict(stats or {})
    fields.update(
        status=status,
        phase="",
        cancel="0",
        # Trimmed: an admin reads this in a Telegram message, and the full
        # traceback is already in the worker log.
        error=error[:400],
        finished_at=time.time(),
    )
    await publish(**fields)


async def cancel() -> bool:
    """Ask a run to stop. False if there is nothing to stop.

    A job still REQUESTED has no worker to read the flag, so it is closed
    here; a RUNNING one is flagged and stops itself at the next batch
    boundary, which is also the last committed checkpoint.
    """
    status = await get_redis().hget(STATE_KEY, "status")
    if status not in ACTIVE:
        return False
    if status == REQUESTED:
        await publish(status=CANCELLED, phase="", finished_at=time.time())
        return True
    await publish(cancel="1")
    return True


async def is_cancelled() -> bool:
    return (await get_redis().hget(STATE_KEY, "cancel")) == "1"
