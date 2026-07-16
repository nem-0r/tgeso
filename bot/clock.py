"""Clock abstraction so the funnel is testable with virtual time."""
import time
import asyncio


class RealClock:
    def now(self) -> int:
        return int(time.time())

    async def sleep(self, seconds: float):
        await asyncio.sleep(seconds)


class VirtualClock:
    """Deterministic clock for simulation / tests; time is advanced externally."""

    def __init__(self, start: int):
        self._t = int(start)

    def now(self) -> int:
        return self._t

    def set(self, t: int):
        self._t = int(t)

    def advance(self, dt: int):
        self._t += int(dt)

    async def sleep(self, seconds: float):
        return  # time driven by the simulation driver
