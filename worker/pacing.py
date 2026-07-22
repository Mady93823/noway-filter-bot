"""Adaptive request pacing (golden rule 8).

Never a single fixed sleep: the delay honors FloodWait retry_after,
backs off exponentially on repeated errors, and decays back toward the
configured floor after sustained success.
"""

import asyncio
import random


class AdaptivePacer:
    def __init__(
        self,
        base_delay: float,
        *,
        max_delay: float = 60.0,
        decay_after: int = 20,
    ) -> None:
        self._floor = base_delay
        self._max = max_delay
        self._decay_after = decay_after
        self._delay = base_delay
        self._success_streak = 0
        self._failure_streak = 0

    @property
    def delay(self) -> float:
        return self._delay

    async def wait(self) -> None:
        """Inter-batch pause, jittered so batches never align exactly."""
        await asyncio.sleep(self._delay + random.uniform(0.0, 0.5))

    def record_success(self) -> None:
        self._failure_streak = 0
        self._success_streak += 1
        if self._success_streak >= self._decay_after and self._delay > self._floor:
            self._delay = max(self._floor, self._delay * 0.9)
            self._success_streak = 0

    async def on_flood_wait(self, retry_after: float) -> None:
        """Honor Telegram's retry_after fully, then stay slower afterwards."""
        self._success_streak = 0
        self._delay = min(self._max, self._delay * 1.5)
        await asyncio.sleep(retry_after + random.uniform(1.0, 3.0))

    async def on_error(self) -> None:
        """Exponential backoff for repeated non-FloodWait errors."""
        self._success_streak = 0
        self._failure_streak += 1
        backoff = min(self._max, self._delay * (2**self._failure_streak))
        await asyncio.sleep(backoff)
