"""Locks that name their holder: one MCP session per state directory, one controller per phone.

The kernel drops an ``flock`` when its holder dies, however it dies, so a lock left
by a killed process never blocks. The file names the holder so a later controller
can tell a live one from one that is cleaning up, and say who it is.
"""
import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time

from .core import ConnectionUnavailable

LOCK_POLL_INTERVAL = 0.2
LOCK_DIR_ENV = "IPHONE_AGENT_LOCK_DIR"
_PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

_log = logging.getLogger(__name__)


class HolderLock:
    """An exclusive ``flock`` whose file records who holds it and whether it is still in use."""

    def __init__(self, path, **details):
        self.path = Path(path)
        self._details = details
        self._guard = threading.Lock()
        self._fd = None
        self._state = "active"

    def _write(self):
        os.ftruncate(self._fd, 0)
        os.pwrite(self._fd, json.dumps({"pid": os.getpid(), "state": self._state, **self._details}).encode(), 0)

    def _try_acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        with self._guard:
            self._fd = fd
            try:
                self._write()
            except BaseException:
                # A holder that cannot name itself would look like one still closing to everyone else.
                self._fd = None
                os.close(fd)
                raise
            self._held()
        return True

    def _held(self):
        pass

    def _holder(self):
        try:
            holder = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}  # The holder is between taking the lock and naming itself.
        return holder if isinstance(holder, dict) else {}

    def _busy(self, holder):
        raise NotImplementedError

    def _still_closing(self, holder, timeout):
        raise NotImplementedError

    def _waiting(self, holder):
        pass

    async def acquire(self, timeout):
        """Wait out a holder that is cleaning up; refuse a live one at once. Return True if there was a wait."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        waited = False
        while not self._try_acquire():
            holder = self._holder()
            if holder.get("state") == "active":
                raise self._busy(holder)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise self._still_closing(holder, timeout)
            if not waited:
                self._waiting(holder)
            waited = True
            await asyncio.sleep(min(LOCK_POLL_INTERVAL, remaining))
        return waited

    def mark_cleaning(self):
        with self._guard:
            self._state = "cleaning"
            if self._fd is not None:
                self._write()

    def release(self):
        with self._guard:
            if self._fd is not None:
                with contextlib.suppress(OSError):
                    os.ftruncate(self._fd, 0)  # A reader must not take a finished holder for a live one.
                os.close(self._fd)
                self._fd = None
                self._released()

    def _released(self):
        pass


class PhoneBusy(ConnectionUnavailable):
    pass


def phone_lock_dir():
    """Shared by every state directory, so controllers started with different ``--home`` still meet."""
    configured = os.environ.get(LOCK_DIR_ENV)
    return Path(configured) if configured else Path.home() / ".iphone-agent" / "phones"


def _file_name(udid):
    if _PLAIN_NAME.fullmatch(udid) and udid not in {".", ".."}:
        return f"{udid}.lock"
    return f"udid-{hashlib.sha256(udid.encode()).hexdigest()[:32]}.lock"


class PhoneLock(HolderLock):
    """One controller per phone: WebDriverAgent keeps one session, and a second controller's ends the first's."""

    _holding = set()
    _holding_guard = threading.Lock()

    def __init__(self, udid, *, mode, home, url=None, idle_timeout=None):
        details = {"udid": udid, "mode": mode, "home": str(home), "since": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        if url is not None:
            details["url"] = url
        if idle_timeout is not None:
            details["idleTimeout"] = idle_timeout
        super().__init__(phone_lock_dir() / _file_name(udid), **details)
        self.udid = udid

    def _held(self):
        with PhoneLock._holding_guard:
            PhoneLock._holding.add(self)

    def _released(self):
        with PhoneLock._holding_guard:
            PhoneLock._holding.discard(self)

    @classmethod
    def mark_held_cleaning(cls):
        """Called from any thread when this process starts to close, so others wait instead of being refused."""
        with cls._holding_guard:
            held = list(cls._holding)
        for lock in held:
            try:
                lock.mark_cleaning()
            except OSError as exc:
                _log.warning("Could not mark the lock of iPhone %s as closing: %s", lock.udid, exc)

    def _who(self, holder):
        return (f"{holder.get('mode', 'unknown mode')}, pid {holder.get('pid')}, "
                f"state directory {holder.get('home', 'unknown')}")

    def _busy(self, holder):
        if holder.get("mode") == "serve":
            command = f"iphone-agent --home {holder.get('home', '~/.iphone-agent')} call shutdown"
            if holder.get("url"):
                command += f" --url {holder['url']}"
            advice = f"Stop it with Ctrl-C in its terminal or `{command}`, then retry."
        else:
            idle = holder.get("idleTimeout")
            if isinstance(idle, (int, float)) and not isinstance(idle, bool) and idle > 0:
                after = f"{idle / 60:g} minutes" if idle >= 60 else f"{idle:g} seconds"
                advice = (f"Close that MCP client session, or wait until it has made no call for {after} "
                          "(its --idle-timeout) and disconnects, then retry.")
            elif idle is None:
                advice = ("Close that MCP client session, or wait until it has made no call for its "
                          "--idle-timeout and disconnects, then retry.")
            else:
                advice = "Close that MCP client session, then retry; it never disconnects on its own (--idle-timeout 0)."
        return PhoneBusy(f"iPhone {self.udid} is in use by another iphone-agent controller ({self._who(holder)}). "
                         f"Two controllers on one phone end each other's WebDriverAgent session. {advice}")

    def _still_closing(self, holder, timeout):
        return PhoneBusy(f"The previous controller of iPhone {self.udid} ({self._who(holder)}) is still closing its "
                         f"connection after {timeout:g}s. Retry in a minute.")

    def _waiting(self, holder):
        _log.warning("Waiting for the previous controller of iPhone %s (%s) to finish its cleanup",
                     self.udid, self._who(holder))
