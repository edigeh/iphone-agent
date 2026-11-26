"""Keep the iPhone awake while an agent is using it, without changing any setting.

iOS restarts its Auto-Lock countdown on every input event. A key event that no app
handles (F13) does that with no visible effect, so the phone stays awake while the
events keep arriving and follows its own Auto-Lock setting again once they stop.
Nothing is written to the phone, so a session that ends or dies leaves nothing to restore.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from typing import Any

# USB HID keyboard page, F13. iOS and its apps give this key no function.
NUDGE_PAGE = 0x07
NUDGE_USAGE = 0x68
NUDGE_SECONDS = 0.02
# The shortest Auto-Lock is 30 seconds, so two events can be lost before the phone locks.
DEFAULT_INTERVAL = 10.0
DEFAULT_IDLE_LIMIT = 30 * 60.0


class ActivityKeepAwake:
    """Send idle-timer nudges until the agent has made no call for ``idle_limit`` seconds.

    ``idle_limit=None`` never stops. ``busy`` reports work that runs without calls, such as a
    recording; nudges continue while it returns true.
    """

    def __init__(self, wda: Any, *, idle_limit: float | None = DEFAULT_IDLE_LIMIT,
                 interval: float = DEFAULT_INTERVAL, busy: Callable[[], bool] | None = None):
        for name, value in (("idle_limit", idle_limit), ("interval", interval)):
            if value is None and name == "idle_limit":
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number of seconds")
        self.wda = wda
        self.idle_limit = None if idle_limit is None else float(idle_limit)
        self.interval = float(interval)
        self._busy = busy
        self._task: asyncio.Task[None] | None = None
        self._last_activity = time.monotonic()
        self._last_nudge: float | None = None
        self._locked: bool | None = None
        self._last_error: str | None = None

    def activity(self) -> None:
        """Record an agent call; the idle limit counts from the latest one."""
        self._last_activity = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity

    @property
    def in_use(self) -> bool:
        """Whether the agent called recently enough, or left something running, to keep the phone awake."""
        return (self.idle_limit is None or self.idle_seconds < self.idle_limit
                or (self._busy is not None and self._busy()))

    @property
    def status(self) -> dict[str, Any]:
        running = self._task is not None and not self._task.done()
        idle = self.idle_seconds
        return {
            "active": running and self.in_use and self._locked is not True,
            "method": "input-nudge",
            "changesSettings": False,
            "intervalSeconds": self.interval,
            "idleLimitSeconds": self.idle_limit,
            "idleSeconds": round(idle, 1),
            "secondsSinceNudge": None if self._last_nudge is None else round(time.monotonic() - self._last_nudge, 1),
            "locked": self._locked,
            "lastError": self._last_error,
        }

    async def open(self) -> "ActivityKeepAwake":
        if self._task is None or self._task.done():
            self.activity()
            await self._attempt()  # The first status an agent reads already says whether nudging works.
            self._task = asyncio.create_task(self._run(), name="iphone-agent-keep-awake")
        return self

    async def nudge(self) -> bool:
        """Send one event unless the phone is locked; a locked screen is left dark."""
        locked = self._locked = await self.wda.locked()
        if locked:
            return False
        await self.wda.device_event(NUDGE_PAGE, NUDGE_USAGE, NUDGE_SECONDS)
        self._last_nudge = time.monotonic()
        return True

    async def _attempt(self) -> None:
        try:
            async with asyncio.timeout(self.interval):
                await self.nudge()
            self._last_error = None
        except Exception as exc:
            # Keep-awake never fails a session. A broken link also fails the agent's next call,
            # which starts the reconnect.
            self._last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            if self.in_use:
                await self._attempt()

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
