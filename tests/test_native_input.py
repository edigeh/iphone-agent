import asyncio
import pytest
import iphone_agent.native_input as module
from iphone_agent.native_input import NativeInput


class Lease:
    def __init__(self): self.acquires = self.releases = 0
    async def acquire(self): self.acquires += 1
    async def release(self): self.releases += 1


@pytest.mark.asyncio
async def test_persistent_hid_lease_and_partial_open_cleanup(monkeypatch):
    lease, events = Lease(), []
    class HID:
        def __init__(self, rsd): pass
        async def connect(self): events.append("connect")
        async def close(self): events.append("close")
        async def create_keyboard_service(self, **kwargs): return 42
        async def send_keyboard(self, sid, keys): events.append(tuple(keys))
    monkeypatch.setattr(module, "UniversalHIDServiceService", HID)
    client = NativeInput(None, lease)
    await client.keyboard([[4]])
    await client.keyboard([[5]])
    assert lease.acquires == 1 and events.count("connect") == 1
    await client.close()
    assert lease.releases == 1 and events[-1] == "close"
    class Broken(HID):
        async def connect(self): raise RuntimeError("failed")
    monkeypatch.setattr(module, "UniversalHIDServiceService", Broken)
    with pytest.raises(RuntimeError, match="failed"):
        await client.open()
    assert lease.acquires == 2 and lease.releases == 2
    assert events[-1] == "close"


@pytest.mark.asyncio
async def test_cancelled_gesture_releases_contacts_and_keeps_shared_lease(monkeypatch):
    lease, reports = Lease(), []
    down = asyncio.Event()
    class HID:
        def __init__(self, rsd): pass
        async def connect(self): pass
        async def close(self): pass
        async def send_multitouch(self, contacts):
            reports.append(contacts)
            down.set()
    monkeypatch.setattr(module, "UniversalHIDServiceService", HID)
    client = NativeInput(None, lease)
    task = asyncio.create_task(client.actions([
        {"type": "pointerDown", "x": 10, "y": 20},
        {"type": "pause", "duration": 20000},
        {"type": "pointerUp", "x": 10, "y": 20},
    ], lambda x, y: (x, y)))
    await down.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert reports[0][0].touching and not reports[-1][0].touching
    assert lease.releases == 0
    await client.close()
    assert lease.releases == 1


@pytest.mark.asyncio
async def test_invalid_whole_gesture_has_no_side_effects():
    lease = Lease()
    client = NativeInput(None, lease)
    with pytest.raises(ValueError, match="release"):
        await client.actions([{"type": "pointerDown", "x": 1, "y": 2}], lambda x, y: (x, y))
    assert lease.acquires == 0


@pytest.mark.asyncio
async def test_multitouch_reports_retain_other_finger(monkeypatch):
    reports = []
    class HID:
        def __init__(self, rsd): pass
        async def connect(self): pass
        async def close(self): pass
        async def send_multitouch(self, contacts): reports.append(contacts)
    monkeypatch.setattr(module, "UniversalHIDServiceService", HID)
    client = NativeInput(None, Lease())
    await client.actions([
        {"type": "pointerDown", "pointerId": 8, "x": 1, "y": 2},
        {"type": "pointerDown", "pointerId": 9, "x": 3, "y": 4},
        {"type": "pointerUp", "pointerId": 8, "x": 1, "y": 2},
        {"type": "pointerUp", "pointerId": 9, "x": 3, "y": 4},
    ], lambda x, y: (x, y))
    assert len(reports[2]) == 2
    assert reports[2][0].touching is False and reports[2][1].touching is True
    assert reports[1][1].identity == reports[3][0].identity
    await client.close()

@pytest.mark.asyncio
async def test_keyboard_cancel_releases_modifier(monkeypatch):
    down, reports = asyncio.Event(), []
    class HID:
        def __init__(self, rsd): pass
        async def connect(self): pass
        async def close(self): pass
        async def create_keyboard_service(self, **kwargs): return 99
        async def send_keyboard(self, sid, keys):
            reports.append(keys)
            if keys:
                down.set()
    monkeypatch.setattr(module, "UniversalHIDServiceService", HID)
    client = NativeInput(None, Lease())
    task = asyncio.create_task(client.keyboard([[0xE3, 4]]))
    await down.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert reports == [[0xE3, 4], []]
    await client.close()


@pytest.mark.asyncio
async def test_unverified_default_media_gate_has_no_device_side_effects():
    class ForbiddenRSD:
        def __getattr__(self, name): pytest.fail("unverified media start must not contact device")
    client = NativeInput(ForbiddenRSD())
    with pytest.raises(RuntimeError, match="verified owned"):
        await client.open()
    assert client.hid is None and not client._leased

@pytest.mark.asyncio
async def test_concurrent_lazy_open_acquires_one_lease(monkeypatch):
    lease = Lease()
    class HID:
        def __init__(self, rsd): pass
        async def connect(self): await asyncio.sleep(0)
        async def close(self): pass
    monkeypatch.setattr(module, "UniversalHIDServiceService", HID)
    client = NativeInput(None, lease)
    await asyncio.gather(client.open(), client.open(), client.open())
    assert lease.acquires == 1
    await client.close()
    assert lease.releases == 1

@pytest.mark.asyncio
async def test_cancelled_keyboard_cleanup_still_closes_hid_and_lease():
    events = []
    class HID:
        async def send_keyboard(self, sid, keys):
            events.append("keyboard")
            raise asyncio.CancelledError()
        async def close(self): events.append("hid")
    class CleanupLease(Lease):
        async def release(self):
            events.append("lease")
            await super().release()
    client = NativeInput(None, CleanupLease())
    client.hid, client.keyboard_id, client._leased = HID(), 99, True
    with pytest.raises(asyncio.CancelledError):
        await client.close()
    assert events == ["keyboard", "hid", "lease"]
    assert client.hid is None and not client._leased


@pytest.mark.asyncio
async def test_failed_cleanup_retains_ownership_for_retry():
    events = []
    class HID:
        fail = True
        async def close(self):
            events.append("hid")
            if self.fail: raise RuntimeError("hid close failed")
    class CleanupLease(Lease):
        fail = True
        async def release(self):
            events.append("lease")
            if self.fail: raise asyncio.CancelledError()
            await super().release()
    lease, hid = CleanupLease(), HID()
    client = NativeInput(None, lease)
    client.hid, client._leased = hid, True
    with pytest.raises(BaseExceptionGroup): await client.close()
    assert events == ["hid", "lease"]
    assert client.hid is hid and client._leased
    with pytest.raises(RuntimeError, match="cleanup"):
        await client.open()
    hid.fail = lease.fail = False
    await client.close()
    assert events == ["hid", "lease", "hid", "lease"]
    assert client.hid is None and not client._leased
