"""Persistent HID input with an explicitly owned media authentication lease."""
from __future__ import annotations

import asyncio
import secrets
from typing import Protocol

from pymobiledevice3.remote.core_device.hid_service import (
    UniversalHIDServiceService, TouchscreenContact,
)


class InputLease(Protocol):
    """Capture managers may share their stream by reference-counting this interface."""
    async def acquire(self) -> None: ...
    async def release(self) -> None: ...


class OwnedMediaGate:
    """Own only the stream created for this gate; never stop another client's stream."""
    def __init__(self, rsd):
        self.rsd = rsd
        self.display = None
        self.transport = None
        self.task = None
        self.token = None

    async def acquire(self):
        raise RuntimeError("Native input requires a verified owned media-stream lease")

    async def release(self):
        errors = []
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                errors.append(exc)
            self.task = None
        if self.display and self.token is not None:
            try:
                async with asyncio.timeout(5):
                    await self.display.stop_media_stream(stop_all=False, identifiers=[self.token])
            except BaseException as exc:
                errors.append(exc)
            self.token = None
        if self.transport:
            self.transport.close()
            self.transport = None
        if self.display:
            try:
                await self.display.close()
            except BaseException as exc:
                errors.append(exc)
            self.display = None
        if errors:
            raise BaseExceptionGroup("Owned input stream cleanup failed", errors)


class NativeInput:
    def __init__(self, rsd, lease: InputLease | None = None):
        self.rsd = rsd
        self.lease = lease or OwnedMediaGate(rsd)
        self.available = lease is not None
        self.hid = None
        self.keyboard_id = None
        self._leased = False
        self._cleanup_pending = False
        self._lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()

    async def open(self):
        async with self._lifecycle_lock:
            await self._open()

    async def _open(self):
        if self._cleanup_pending:
            raise RuntimeError("Native HID cleanup must complete before reopening")
        if self.hid:
            return
        self._leased = True
        try:
            await self.lease.acquire()
            self.hid = UniversalHIDServiceService(self.rsd)
            await self.hid.connect()
        except BaseException as primary:
            try:
                await self._close()
            except BaseException as cleanup:
                raise BaseExceptionGroup("Native HID setup and cleanup failed", [primary, cleanup]) from None
            raise

    async def close(self):
        async with self._lifecycle_lock:
            await self._close()

    async def _close(self):
        errors = []
        self._cleanup_pending = bool(self.hid or self._leased)
        hid = self.hid
        if hid:
            if self.keyboard_id is not None:
                try:
                    await hid.send_keyboard(self.keyboard_id, [])
                except BaseException as exc:
                    errors.append(exc)
                else:
                    self.keyboard_id = None
            try:
                await hid.close()
            except BaseException as exc:
                errors.append(exc)
            else:
                self.hid = None
                self.keyboard_id = None
        if self._leased:
            try:
                await self.lease.release()
            except BaseException as exc:
                errors.append(exc)
            else:
                self._leased = False
        self._cleanup_pending = bool(self.hid or self._leased)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Native HID cleanup failed", errors)

    async def keyboard(self, usages):
        async with self._lock:
            await self.open()
            if self.keyboard_id is None:
                # Distinct virtual surface per owner; service IDs keep bit 32 set.
                service_id = 0x100000000 | secrets.randbits(32)
                self.keyboard_id = await self.hid.create_keyboard_service(service_id=service_id)
            for keys in usages:
                try:
                    await self.hid.send_keyboard(self.keyboard_id, keys)
                    await asyncio.sleep(0.02)
                finally:
                    await self.hid.send_keyboard(self.keyboard_id, [])
                await asyncio.sleep(0.02)

    async def actions(self, actions, point):
        if not isinstance(actions, list) or not 1 <= len(actions) <= 4096:
            raise ValueError("Provide 1–4096 touch actions")
        # Validate the entire gesture before sending its first down report.
        active, normalized, duration = {}, [], 0.0
        for event in actions:
            kind = event.get("type")
            pointer = event.get("pointerId", 0)
            millis = float(event.get("duration", 0))
            if not 0 <= millis <= 30_000:
                raise ValueError("invalid gesture duration")
            duration += millis
            if duration > 30_000:
                raise ValueError("gesture exceeds 30 seconds")
            if kind == "pause":
                normalized.append((kind, pointer, None, millis))
                continue
            if kind not in {"pointerDown", "pointerMove", "pointerUp"}:
                raise ValueError("unknown pointer action")
            position = point(event["x"], event["y"])
            if kind == "pointerDown":
                if pointer in active or len(active) >= 5:
                    raise ValueError("duplicate pointer or more than five contacts")
                active[pointer] = position
            else:
                if pointer not in active:
                    raise ValueError("pointer action without preceding down")
                if kind == "pointerUp":
                    del active[pointer]
            normalized.append((kind, pointer, position, millis))
        if active:
            raise ValueError("gesture must release every pointer")
        async with self._lock:
            await self.open()
            active = {}
            try:
                for kind, pointer, position, millis in normalized:
                    if kind == "pause":
                        await asyncio.sleep(millis / 1000)
                        continue
                    if kind == "pointerDown":
                        identity = next(i for i in range(5) if i not in {c.identity for c in active.values()})
                        active[pointer] = TouchscreenContact(identity, *position)
                    elif kind == "pointerMove":
                        start = active[pointer]
                        steps = max(1, round(millis / 16))
                        for step in range(1, steps + 1):
                            active[pointer] = TouchscreenContact(start.identity,
                                round(start.x + (position[0] - start.x) * step / steps),
                                round(start.y + (position[1] - start.y) * step / steps))
                            await self.hid.send_multitouch(list(active.values()))
                            if millis:
                                await asyncio.sleep(millis / steps / 1000)
                        continue
                    else:
                        old = active[pointer]
                        active[pointer] = TouchscreenContact(old.identity, *position, touching=False)
                    await self.hid.send_multitouch(list(active.values()))
                    if kind == "pointerUp":
                        del active[pointer]
            finally:
                if active:
                    await self.hid.send_multitouch([c._replace(touching=False) for c in active.values()])
