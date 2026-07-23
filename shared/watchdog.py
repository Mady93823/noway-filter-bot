"""Dependency watchdog: DM an admin when Postgres or Redis goes down.

The rate governor, search cache, access clock and pagination all read
Redis; searches and deliveries read Postgres. When either becomes
unreachable the bot degrades quietly - the governor fails open, caches
miss - and nobody watching the logs would notice until users complain.

This loop closes that gap. It reuses the same reachability probe the
health endpoint uses, but pushes the result to the admin's PM instead of
waiting to be polled.

Edge-triggered on purpose: it alerts only when a component CHANGES state
(healthy -> down, down -> recovered), so a lasting outage is one DM, not
one per tick. That also means the decision needs no Redis dedupe - which
matters, because the whole point is to still work when Redis is the thing
that is down. notify_admins sends the DM directly and the governor fails
open, so a Redis outage can still be reported.

A short consecutive-failure threshold rides out a single transient blip
before crying wolf.
"""

import asyncio
import logging

from shared.alerts import notify_admins
from shared.health import _probe

logger = logging.getLogger(__name__)

_FAIL_THRESHOLD = 2  # consecutive bad probes before declaring a component down

_LABELS = {"db": "Postgres", "redis": "Redis"}
_DOWN_DETAIL = {
    "db": "Searches and deliveries cannot run until it is back.",
    "redis": (
        "Search cache, pagination, the access clock and the rate governor "
        "all use Redis - the bot is degraded and the governor is failing open."
    ),
}


class _Health:
    __slots__ = ("healthy", "fails")

    def __init__(self) -> None:
        self.healthy = True
        self.fails = 0


async def watchdog(client, interval: int) -> None:
    """Poll dependencies forever; DM admins on every state change."""
    state = {name: _Health() for name in _LABELS}
    logger.info("dependency watchdog started (every %ss)", interval)
    # Let startup settle so a cold engine/redis does not false-alarm.
    await asyncio.sleep(interval)
    while True:
        try:
            checks = await _probe()
            for name, ok in checks.items():
                comp = state[name]
                if ok:
                    if not comp.healthy:
                        await notify_admins(
                            client,
                            f"🟢 {_LABELS[name]} recovered — back online.",
                        )
                    comp.healthy = True
                    comp.fails = 0
                else:
                    comp.fails += 1
                    if comp.healthy and comp.fails >= _FAIL_THRESHOLD:
                        comp.healthy = False
                        await notify_admins(
                            client,
                            f"🔴 {_LABELS[name]} is unreachable.\n{_DOWN_DETAIL[name]}",
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            # The loop surviving a bad tick matters more than the tick.
            logger.exception("watchdog tick failed")
        await asyncio.sleep(interval)
