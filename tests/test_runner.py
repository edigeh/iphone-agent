import asyncio
from types import SimpleNamespace

import aiohttp
import pytest

import iphone_agent.runner as runner_module
from iphone_agent.runner import RunnerError, WDARunner


@pytest.fixture
def fake_launch(monkeypatch):
    state = SimpleNamespace(
        usb=True, paired=True, rsd_udid="device-1", existing=False,
        runner_started=False, runner_failed=False, runner_stopped=False,
        tunnel_closed=False, forward_closed=False, wda_closed=False, config=None,
        expected_port=8100,
    )

    class FakeMuxDevice:
        serial = "device-1"
        connection_type = "USB"

        def matches_udid(self, udid):
            return udid == self.serial

    async def list_devices():
        return [FakeMuxDevice()] if state.usb else []

    class FakeLockdown:
        @property
        def paired(self):
            return state.paired

        async def close(self):
            pass

    async def create_using_usbmux(**kwargs):
        assert kwargs["serial"] == "device-1"
        assert kwargs["connection_type"] == "USB"
        assert kwargs["autopair"] is False
        return FakeLockdown()

    class FakeForward:
        def __init__(self, udid, port):
            assert udid == "device-1" and port == state.expected_port
            self.port = 4321

        async def open(self):
            return self

        async def close(self):
            state.forward_closed = True

    class FakeWDA:
        def __init__(self, url, *, timeout):
            assert url == "http://127.0.0.1:4321" and timeout == 2.0

        async def call(self, method, path):
            assert (method, path) == ("GET", "/status")
            await asyncio.sleep(0)
            if not (state.existing or state.runner_started):
                raise aiohttp.ClientConnectionError("device port closed")
            product = "other.bundle" if state.existing == "wrong" else "com.example.WDARunner"
            return {"ready": True, "state": "success", "build": {"productBundleIdentifier": product}}

        async def close(self):
            state.wda_closed = True

    class FakeRSD:
        @property
        def udid(self):
            return state.rsd_udid

    class FakeTunnel:
        def __init__(self, **kwargs):
            assert kwargs == {"serial": "device-1", "autopair": False, "remotepairing_fallback": False}

        async def aopen(self):
            return FakeRSD()

        async def aclose(self):
            state.tunnel_closed = True

    class FakeConfig:
        @classmethod
        async def create_for(cls, rsd, *, runner_bundle_id):
            assert rsd.udid == "device-1"
            assert runner_bundle_id == "com.example.WDARunner.xctrunner"
            state.config = SimpleNamespace(runner_app_env=None)
            return state.config

    class FakeTestService:
        def __init__(self, rsd):
            assert rsd.udid == "device-1"

        async def run(self, config):
            assert config is state.config
            state.runner_started = True
            if state.runner_failed:
                raise RuntimeError("testmanagerd rejected runner")
            try:
                await asyncio.Event().wait()
            finally:
                state.runner_stopped = True

    monkeypatch.setattr(runner_module, "list_devices", list_devices)
    monkeypatch.setattr(runner_module, "create_using_usbmux", create_using_usbmux)
    monkeypatch.setattr(runner_module, "USBForward", FakeForward)
    monkeypatch.setattr(runner_module, "WDAClient", FakeWDA)
    monkeypatch.setattr(runner_module, "USBUserspaceRsdTunnel", FakeTunnel)
    monkeypatch.setattr(runner_module, "TestConfig", FakeConfig)
    monkeypatch.setattr(runner_module, "XCUITestService", FakeTestService)
    return state


@pytest.mark.asyncio
async def test_starts_signed_xctest_and_keeps_resources_until_close(fake_launch):
    runner = WDARunner("device-1", "com.example.WDARunner.xctrunner")
    await runner.open()
    assert runner.status["ready"] is True
    assert runner.task is not None and not runner.task.done()
    assert fake_launch.config.runner_app_env == {
        "WDA_PRODUCT_BUNDLE_IDENTIFIER": "com.example.WDARunner", "USE_PORT": "8100", "USE_IP": "127.0.0.1"
    }
    assert fake_launch.runner_started
    await runner.close()
    assert fake_launch.runner_stopped and fake_launch.tunnel_closed
    assert fake_launch.forward_closed and fake_launch.wda_closed


@pytest.mark.asyncio
async def test_adopts_only_matching_ready_bundle_without_stopping_it(fake_launch):
    fake_launch.existing = True
    runner = WDARunner("device-1", "com.example.WDARunner.xctrunner")
    await runner.open()
    assert runner.adopted and runner.task is None and runner.tunnel is None
    await runner.close()
    assert fake_launch.forward_closed and fake_launch.wda_closed
    assert not fake_launch.runner_stopped


@pytest.mark.asyncio
async def test_rejects_unrelated_wda_and_cleans_forward(fake_launch):
    fake_launch.existing = "wrong"
    runner = WDARunner("device-1", "com.example.WDARunner.xctrunner")
    with pytest.raises(RunnerError, match="other.bundle"):
        await runner.open()
    assert fake_launch.forward_closed and fake_launch.wda_closed
    assert runner.tunnel is None and runner.task is None


@pytest.mark.asyncio
async def test_rsd_mismatch_and_testmanager_failure_cleanup(fake_launch):
    fake_launch.rsd_udid = "other-device"
    runner = WDARunner("device-1", "com.example.WDARunner.xctrunner")
    with pytest.raises(RunnerError, match="RSD handshake"):
        await runner.open()
    assert fake_launch.tunnel_closed and fake_launch.forward_closed

    fake_launch.rsd_udid = "device-1"
    fake_launch.tunnel_closed = fake_launch.forward_closed = False
    fake_launch.runner_failed = True
    runner = WDARunner("device-1", "com.example.WDARunner.xctrunner")
    with pytest.raises(RuntimeError, match="testmanagerd rejected"):
        await runner.open()
    assert fake_launch.tunnel_closed and fake_launch.forward_closed


@pytest.mark.asyncio
async def test_timeout_and_usb_preflight_are_fail_closed(fake_launch):
    fake_launch.usb = False
    runner = WDARunner("device-1", "com.example.WDARunner.xctrunner")
    with pytest.raises(RunnerError, match="USB iPhone"):
        await runner.open()
    assert runner.forward is None

    fake_launch.usb = True
    fake_launch.paired = False
    with pytest.raises(RunnerError, match="not paired"):
        await runner.open()
    assert runner.forward is None

    fake_launch.paired = True
    # Force probes to fail even after the task has started.
    fake_launch.runner_started = False
    original_wda = runner_module.WDAClient

    class NeverReady(original_wda):
        async def call(self, method, path):
            raise aiohttp.ClientConnectionError("device port closed")

    from pytest import MonkeyPatch
    patch = MonkeyPatch()
    patch.setattr(runner_module, "WDAClient", NeverReady)
    try:
        with pytest.raises(TimeoutError, match="did not become ready"):
            await runner.open(timeout=0.01)
    finally:
        patch.undo()
    assert fake_launch.runner_stopped and fake_launch.tunnel_closed
    assert fake_launch.forward_closed and fake_launch.wda_closed


@pytest.mark.asyncio
async def test_cancel_during_tunnel_open_closes_forward_and_tunnel(fake_launch, monkeypatch):
    entered = asyncio.Event()

    class SlowTunnel:
        def __init__(self, **kwargs):
            assert kwargs["serial"] == "device-1"

        async def aopen(self):
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            fake_launch.tunnel_closed = True

    monkeypatch.setattr(runner_module, "USBUserspaceRsdTunnel", SlowTunnel)
    runner = WDARunner("device-1", "com.example.WDARunner.xctrunner")
    opening = asyncio.create_task(runner.open())
    await entered.wait()
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert fake_launch.tunnel_closed and fake_launch.forward_closed and fake_launch.wda_closed


@pytest.mark.asyncio
async def test_startup_timeout_also_bounds_tunnel_establishment(fake_launch, monkeypatch):
    class HangingTunnel:
        def __init__(self, **kwargs):
            assert kwargs["serial"] == "device-1"

        async def aopen(self):
            await asyncio.Event().wait()

        async def aclose(self):
            fake_launch.tunnel_closed = True

    monkeypatch.setattr(runner_module, "USBUserspaceRsdTunnel", HangingTunnel)
    runner = WDARunner("device-1", "com.example.WDARunner.xctrunner")
    with pytest.raises(TimeoutError, match="did not become ready"):
        await asyncio.wait_for(runner.open(timeout=0.01), timeout=1)
    assert fake_launch.tunnel_closed and fake_launch.forward_closed and fake_launch.wda_closed
    assert runner.task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["metadata", "xctest"])
async def test_nested_timeout_keeps_its_cause_instead_of_claiming_startup_deadline(fake_launch, monkeypatch, phase):
    failure = TimeoutError(f"{phase} transport timed out")

    async def fail(*args, **kwargs):
        raise failure

    if phase == "metadata":
        monkeypatch.setattr(runner_module.TestConfig, "create_for", fail)
    else:
        monkeypatch.setattr(runner_module.XCUITestService, "run", fail)
    runner = WDARunner("device-1", "com.example.WDARunner.xctrunner")
    with pytest.raises(TimeoutError) as caught:
        await runner.open(timeout=600)
    assert caught.value is failure
    assert fake_launch.tunnel_closed and fake_launch.forward_closed and fake_launch.wda_closed
    assert runner.task is None


@pytest.mark.asyncio
async def test_cleanup_attempts_http_and_forward_if_tunnel_close_fails(fake_launch, monkeypatch):
    runner = WDARunner("device-1", "com.example.WDARunner.xctrunner")
    await runner.open()

    async def fail_close():
        raise RuntimeError("tunnel close failed")

    monkeypatch.setattr(runner.tunnel, "aclose", fail_close)
    with pytest.raises(RunnerError, match="cleanup failed"):
        await runner.close()
    assert fake_launch.runner_stopped
    assert fake_launch.wda_closed and fake_launch.forward_closed


@pytest.mark.asyncio
async def test_custom_device_port_is_forwarded_and_set_in_runner_env(fake_launch):
    fake_launch.expected_port = 8200
    runner = WDARunner("device-1", "com.example.WDARunner.xctrunner", port=8200)
    await runner.open()
    assert fake_launch.config.runner_app_env["USE_PORT"] == "8200"
    await runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "usb-gone", "provider", "rsd", "provider-close", "identity", "pairing"])
async def test_usb_tunnel_pins_selection_and_unwinds_every_owned_resource(monkeypatch, failure):
    """Exercise the real tunnel lifecycle with no device or socket connections."""
    from contextlib import asynccontextmanager
    import pymobiledevice3.remote.userspace_tunnel as upstream

    events = []
    never = asyncio.Event()
    class Lockdown:
        udid = "wrong" if failure == "identity" else "device-1"
        paired = failure != "pairing"
        async def close(self): events.append("lockdown-close")
    async def select(**kwargs):
        assert kwargs["serial"] == "device-1" and kwargs["autopair"] is False
        # Model the same UDID exposed over both transports. Without a restriction,
        # selection can return Network after USB disappears following preflight.
        selected = kwargs.get("connection_type", "Network")
        events.append("select-" + selected)
        if selected == "USB" and failure == "usb-gone":
            raise ConnectionError("USB disconnected; Network still available")
        return Lockdown()
    async def forbidden_selection(*args, **kwargs):
        pytest.fail("upstream unconstrained USB/Network selection must not run")
    class Tun:
        def set_peer(self, address): assert address == "fd00::1"
    class Transport:
        tun = Tun()
        async def wait_closed(self): await never.wait()
    class Provider:
        @classmethod
        async def create(cls, lockdown):
            assert isinstance(lockdown, Lockdown)
            events.append("provider-create")
            if failure == "provider": raise RuntimeError("provider failed")
            return cls()
        @asynccontextmanager
        async def start_tcp_tunnel(self):
            events.append("transport-open")
            try:
                yield SimpleNamespace(client=Transport(), address="fd00::1", port=1234, auxiliary_metadata={})
            finally:
                events.append("transport-close")
        async def close(self):
            events.append("provider-close")
            if failure == "provider-close": raise RuntimeError("provider close failed")
    class DialPlane:
        def __init__(self, tun, address): assert address == "fd00::1"
        async def __aenter__(self): return self
        async def __aexit__(self, *args): events.append("dial-close")
        async def dial(self, *args, **kwargs): pytest.fail("no network I/O")
    class RSD:
        def __init__(self, address, *, open_connection, auxiliary_metadata):
            assert address == ("fd00::1", 1234) and open_connection.__self__.__class__ is DialPlane
        async def connect(self):
            events.append("rsd-connect")
            if failure == "rsd": raise RuntimeError("RSD failed")
        async def close(self): events.append("rsd-close")
    monkeypatch.setattr(runner_module, "create_using_usbmux", select)
    monkeypatch.setattr(upstream, "create_using_usbmux", forbidden_selection)
    monkeypatch.setattr(runner_module, "CoreDeviceTunnelProxy", Provider)
    monkeypatch.setattr(runner_module, "UserspaceDialPlane", DialPlane)
    monkeypatch.setattr(runner_module, "RemoteServiceDiscoveryService", RSD)
    tunnel = runner_module.USBUserspaceRsdTunnel(serial="device-1", autopair=False, remotepairing_fallback=False)
    if failure in {"usb-gone", "provider", "rsd", "identity", "pairing"}:
        with pytest.raises((RuntimeError, ConnectionError)):
            await tunnel.aopen()
    else:
        await tunnel.aopen()
        assert upstream._active_tunnel is tunnel and upstream.USERSPACE_ACTIVE
        if failure == "provider-close":
            with pytest.raises(RuntimeError, match="provider close"):
                await tunnel.aclose()
        else:
            await tunnel.aclose()
    assert events[0] == "select-USB"
    if failure == "usb-gone":
        assert events == ["select-USB"]
    else:
        assert events[-1] == "lockdown-close"
    if failure in {None, "rsd", "provider-close"}:
        assert events[-5:] == ["rsd-close", "dial-close", "transport-close", "provider-close", "lockdown-close"]
    assert tunnel.rsd is None and tunnel.tun is None
    assert upstream._active_tunnel is None and not upstream.USERSPACE_ACTIVE
    assert not upstream.tunnel_service.USE_USERSPACE_TUNNEL
