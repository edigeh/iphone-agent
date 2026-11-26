import asyncio

import pytest

import iphone_agent.native as native
from iphone_agent.native import NativeClient, NativeUnsupported


class FakeRSD:
    def __init__(self, udid="test-udid", version="27.0", connected=True, services=None):
        self.udid, self.product_version = udid, version
        self.services = {s: {"Port": 1234} for group in NativeClient.ACTION_SERVICES.values() for s in group} if services is None else services
        self.peer_info = {"Services": self.services} if connected else None
        self.connect_count = self.close_count = 0
    async def connect(self):
        self.connect_count += 1
        self.peer_info = {"Services": self.services}
    async def close(self):
        self.close_count += 1


@pytest.mark.asyncio
async def test_device_gate_and_rsd_ownership():
    wrong = FakeRSD(udid="other", connected=False)
    with pytest.raises(RuntimeError, match="does not match"):
        await NativeClient(wrong, udid="test-udid").open()
    assert (wrong.connect_count, wrong.close_count) == (1, 1)
    old = FakeRSD(version="26.6.2")
    with pytest.raises(NativeUnsupported, match="iOS 27"):
        await NativeClient(old, udid="test-udid").open()
    assert old.close_count == 0
    ready = FakeRSD()
    async with NativeClient(ready, udid="test-udid"):
        pass
    assert ready.close_count == 0


@pytest.mark.asyncio
async def test_partial_rsd_connection_is_closed_on_cancel():
    rsd = FakeRSD(connected=False)
    async def connect():
        raise asyncio.CancelledError()
    rsd.connect = connect
    with pytest.raises(asyncio.CancelledError):
        await NativeClient(rsd, udid="test-udid").open()
    assert rsd.close_count == 1


@pytest.mark.asyncio
async def test_service_presence_and_capability_truth():
    client = NativeClient(FakeRSD(services={}), udid="test-udid")
    with pytest.raises(NativeUnsupported, match="not advertised"):
        await client.screenshot()
    assert client.capability_report()["implemented"] == []
    assert "proxy_connect" in client.capability_report()["unavailable"]


def test_remoted_endpoint_uses_apple_identity(monkeypatch):
    identity = object()
    monkeypatch.setattr(native, "remoted_handshake_uuid", lambda: identity)
    captured = {}
    def rsd(address, **kwargs):
        captured.update(address=address, **kwargs)
        return FakeRSD()
    monkeypatch.setattr(native, "RemoteServiceDiscoveryService", rsd)
    NativeClient.from_endpoint("fe80::1%en0", 1234, udid="test-udid", remoted=True)
    assert captured["handshake_uuid"] is identity
    NativeClient.from_endpoint("fd00::1", 1234, udid="test-udid", remoted=False)
    assert captured["handshake_uuid"] is None


@pytest.mark.asyncio
async def test_geometry_must_come_from_fresh_complete_ax_even_with_cli_dimensions():
    client = NativeClient(FakeRSD(), udid="test-udid", width_points=1, height_points=2)
    events = []
    async def snapshot():
        return {"a11y_tree": {"boundsInScreen": {"left": 0, "top": 0, "right": 390, "bottom": 844}}, "truncated": False, "interfaceOrientation": "portrait", "generation": 1}
    async def actions(items, point):
        events.append(point(items[0]["x"], items[0]["y"]))
    client.ax.snapshot = snapshot
    client.input.actions = actions
    await client.tap(195, 422)
    assert events == [(32851, 32806)]
    for point in [(float("nan"), 0), (float("inf"), 0), (391, 0)]:
        with pytest.raises(ValueError):
            client._point(*point)


def test_deep_link_exact_launch_input():
    result = NativeClient.launch_input("example.fixture", "agentcheck://fixture?x=é")
    assert result["options"]["payloadURL"] == {"relative": "agentcheck://fixture?x=é"}
    assert result["options"]["user"] == {"active": True}
    assert result["options"]["platformSpecificOptions"][:8] == b"bplist00"
    assert result["options"]["terminateExisting"] is False
    assert "payloadURL" not in NativeClient.launch_input("example.fixture")["options"]


@pytest.mark.asyncio
async def test_orientation_read_never_rotates_and_set_is_absolute(monkeypatch):
    events = []
    class Service:
        def __init__(self, rsd):
            self.service = self
        async def connect(self): pass
        async def close(self): events.append("close")
        async def send_receive_request(self, request):
            events.append(request)
            return {"currentDeviceOrientation": "faceUp", "currentDeviceNonFlatOrientation": "portrait", "currentDeviceOrientationLocked": True}
    Service.SERVICE_NAME = native.OrientationService.SERVICE_NAME
    monkeypatch.setattr(native, "OrientationService", Service)
    client = NativeClient(FakeRSD(), udid="test-udid")
    assert await client.orientation() == {"orientation": "portrait", "locked": True}
    assert events[0]["payload"] == {"currentOrientation": {}}
    await client.orientation("landscapeRight")
    assert events[2]["payload"] == {"changeOrientation": {"_0": "landscapeRight"}}


@pytest.mark.asyncio
async def test_service_failed_enter_still_closes(monkeypatch):
    events = []
    class Broken:
        SERVICE_NAME = native.ScreenCaptureService.SERVICE_NAME
        def __init__(self, rsd): pass
        async def connect(self): raise RuntimeError("half-open")
        async def close(self): events.append("closed")
    monkeypatch.setattr(native, "ScreenCaptureService", Broken)
    with pytest.raises(RuntimeError, match="half-open"):
        await NativeClient(FakeRSD(), udid="test-udid").screenshot()
    assert events == ["closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("user_changed", [False, True])
async def test_unicode_clipboard_restores_only_owned_change(user_changed):
    client = NativeClient(FakeRSD(), udid="test-udid")
    changes, keys = [], []
    old = {"pasteboard": {"items": [native.text_item("keep me")], "metadata": {"nonce": "a", "changeCount": 1}}}
    owned = {"pasteboard": {"items": [native.text_item("olá")], "metadata": {"nonce": "a", "changeCount": 2}}}
    current = {"pasteboard": {"items": [], "metadata": {"nonce": "a", "changeCount": 3}}} if user_changed else owned
    values = iter([old, current])
    async def snapshot(*args): return next(values)
    async def set_clipboard(items, metadata=None):
        changes.append(items)
        return owned
    async def keyboard(usages): keys.append(usages)
    async def opened(): pass
    client._clipboard_snapshot, client._clipboard_set, client._keyboard = snapshot, set_clipboard, keyboard
    client.input.open = opened
    await client.type_text("olá")
    assert keys == [[[native.KEY_LEFT_GUI, native.KEY_V]]]
    assert len(changes) == (1 if user_changed else 2)
    if not user_changed:
        assert changes[-1] == old["pasteboard"]["items"]


@pytest.mark.asyncio
async def test_unicode_refuses_unpreservable_clipboard_before_writing():
    client = NativeClient(FakeRSD(), udid="test-udid")
    async def opened(): pass
    async def snapshot(): return {"pasteboard": {"items": [{"types": ["public.rtf"], "data": {"public.rtf": {"isPromised": True}}}], "metadata": {"nonce": "a", "changeCount": 1}}}
    client.input.open, client._clipboard_snapshot = opened, snapshot
    with pytest.raises(NativeUnsupported, match="promised"):
        await client.type_text("é")

@pytest.mark.asyncio
async def test_owned_rsd_reconnects_after_close():
    rsd = FakeRSD(connected=False)
    client = NativeClient(rsd, udid="test-udid")
    await client.open()
    await client.close()
    await client.open()
    assert rsd.connect_count == 2
    await client.close()
    assert rsd.close_count == 2


@pytest.mark.asyncio
async def test_cleanup_attempts_ax_and_rsd_after_input_error():
    client = NativeClient(FakeRSD(connected=False), udid="test-udid")
    await client.open()
    events = []
    async def broken(): raise RuntimeError("HID release failed")
    async def ax_close(): events.append("AX closed")
    client.input.close, client.ax.close = broken, ax_close
    with pytest.raises(ExceptionGroup, match="cleanup"):
        await client.close()
    assert events == ["AX closed"] and client.rsd.close_count == 1


@pytest.mark.asyncio
async def test_invalid_text_with_clear_does_not_erase():
    client = NativeClient(FakeRSD(), udid="test-udid")
    async def unexpected(*args): pytest.fail("must not send input")
    client._keyboard = unexpected
    with pytest.raises(ValueError):
        await client.dispatch("type_text", {"text": 123, "clear": True})


def test_default_gate_blocker_is_visible_in_capabilities():
    report = NativeClient(FakeRSD(), udid="test-udid").capability_report()
    assert "tap" not in report["implemented"]
    assert "verified targeted stop" in report["unavailable"]["tap"]
    assert "screenshot" in report["implemented"]


def geometry_state(orientation, width, height):
    return {"a11y_tree": {"boundsInScreen": {"left": 0, "top": 0, "right": width, "bottom": height}},
            "interfaceOrientation": orientation, "generation": 12, "truncated": False}


@pytest.mark.parametrize("orientation,extent,corners", [
    ("portrait", (390, 844), [(0, 0), (65535, 0), (0, 65535), (65535, 65535)]),
    ("portraitUpsideDown", (390, 844), [(65535, 65535), (0, 65535), (65535, 0), (0, 0)]),
    ("landscapeRight", (844, 390), [(0, 65535), (0, 0), (65535, 65535), (65535, 0)]),
    ("landscapeLeft", (844, 390), [(65535, 0), (65535, 65535), (0, 0), (0, 65535)]),
])
def test_portrait_normalization_from_atomic_interface_geometry(orientation, extent, corners):
    client = NativeClient(FakeRSD(), udid="test-udid")
    width, height = extent
    client._geometry_from_state(geometry_state(orientation, width, height))
    assert [client._point(x, y) for x, y in [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]] == corners
    with pytest.raises(ValueError): client._point(width, height - 1)


@pytest.mark.parametrize("orientation,extent", [(None, (390, 844)), (1, (390, 844)),
    ("portrait", (844, 390)), ("landscapeRight", (390, 844)), ("faceUp", (390, 844))])
def test_unproven_or_inconsistent_orientation_fails_closed(orientation, extent):
    client = NativeClient(FakeRSD(), udid="test-udid")
    client._geometry_from_state(geometry_state("portrait", 390, 844))
    with pytest.raises(NativeUnsupported):
        client._geometry_from_state(geometry_state(orientation, *extent))
    with pytest.raises(NativeUnsupported): client._point(1, 2)

@pytest.mark.asyncio
async def test_touch_validation_uses_immutable_geometry_snapshot():
    client = NativeClient(FakeRSD(), udid="test-udid")
    async def snapshot(): return geometry_state("portrait", 390, 844)
    async def actions(items, normalize):
        client._geometry_from_state(geometry_state("landscapeRight", 844, 390))
        assert normalize(items[0]["x"], items[0]["y"]) == (65535, 65535)
    client.ax.snapshot, client.input.actions = snapshot, actions
    await client.tap(389, 843)


@pytest.mark.asyncio
async def test_bad_orientation_fails_before_input_or_lease():
    client = NativeClient(FakeRSD(), udid="test-udid")
    async def snapshot(): return geometry_state(1, 390, 844)
    async def forbidden(*args): pytest.fail("unproven orientation must not send input")
    client.ax.snapshot, client.input.actions = snapshot, forbidden
    with pytest.raises(NativeUnsupported, match="orientation"):
        await client.tap(10, 20)


@pytest.mark.asyncio
async def test_geometry_refresh_failure_invalidates_previous_transform():
    client = NativeClient(FakeRSD(), udid="test-udid")
    client._geometry_from_state(geometry_state("portrait", 390, 844))
    async def failed(): raise RuntimeError("AX disconnected")
    client.state = failed
    with pytest.raises(RuntimeError): await client.screen_geometry()
    with pytest.raises(NativeUnsupported): client._point(10, 20)
