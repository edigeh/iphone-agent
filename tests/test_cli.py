"""CLI network startup and failure cleanup without a device or Apple services."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from iphone_agent.core import PhoneLocked
from iphone_agent import cli, keepawake, network, apple_runner
from iphone_agent.audit import AuditLog
from iphone_agent.core import Controller
from iphone_agent.wda import WDAError


UDID = "DEVICE-UDID"
BUNDLE = "org.example.WDA.xctrunner"
HOST = "fd00:db8:1::1"


def args(home: Path, *, no_keep_awake=True):
    return SimpleNamespace(
        command="mcp", home=home, udid=UDID, paired_network=True, wda_url=None,
        wda_port=8123, startup_timeout=37.0, runner_bundle_id=BUNDLE, no_keep_awake=no_keep_awake,
        native_rsd_host=None, native_rsd_port=58783, native_width=None, native_height=None,
        sms_backup_dir=None, allow_erase=False,
    )


@pytest.fixture
def fakes(monkeypatch):
    events = []
    state = {"binding": None, "runner": None, "wda": None, "controller": None,
             "bindings": [], "runners": [], "wdas": [], "records": [], "effects": [],
             "hosts": [HOST], "resolve_errors": [], "resolve_error": None,
             "locks": [False], "usb": [False], "nudges": [], "mutation_error": None}

    def next_value(values):
        return values.pop(0) if len(values) > 1 else values[0]

    class Device:
        udid = UDID

        def __init__(self, udid):
            assert udid == UDID

        @staticmethod
        async def discover():
            events.append("discover")
            return []  # No USB device: paired network is explicit.

        async def close(self):
            events.append("device.close")

        async def info(self):
            events.append("device.info")
            return {"UniqueDeviceID": UDID}

        async def usb_present(self):
            events.append("device.usb_present")
            return next_value(state["usb"])

    class Forward:
        host = HOST
        port = 8123

        async def open(self):
            events.append("forward.open")
            return self

        async def close(self):
            events.append("forward.close")

    class Binding:
        host = HOST
        port = 8123
        origin = f"http://[{HOST}]:8123"

        @classmethod
        async def for_device(cls, udid, port):
            assert (udid, port) == (UDID, 8123)
            events.append("binding.for_device")
            error = state["resolve_errors"].pop(0) if state["resolve_errors"] else state["resolve_error"]
            if error:
                raise error
            instance = cls()
            instance.host = next_value(state["hosts"])
            instance.origin = f"http://[{instance.host}]:8123"
            state["binding"] = instance
            state["bindings"].append(instance)
            return instance

        def connection_factory(self, udid, port):
            assert (udid, port) == (UDID, 8123)
            events.append("binding.connection_factory")
            forward = Forward()
            forward.host = self.host
            return forward

    class WDA:
        def __init__(self, binding):
            assert binding is state["binding"]
            self.binding = binding
            self.index = len(state["wdas"])
            state["wdas"].append(self)
            state["wda"] = self
            events.append("wda.init")

        async def open(self):
            events.append("wda.open")

        async def locked(self, *, max_age=0.0):
            events.append("wda.locked")
            return next_value(state["locks"])

        async def device_event(self, page, usage, duration):
            events.append("wda.device_event")
            state["nudges"].append((self.index, page, usage, duration))

        async def call(self, method, path, body=None):
            assert (method, path) == ("GET", "/status")
            events.append("wda.status")
            return {"ready": True, "state": "success",
                    "build": {"productBundleIdentifier": BUNDLE.removesuffix(".xctrunner")}}

        async def dispatch(self, action, params):
            events.append(f"wda.dispatch.{action}")
            if state.get("mutation_error_before_effect") and self.index == 0:
                raise state["mutation_error_before_effect"]
            state["effects"].append((self.index, action, params))
            if state["mutation_error"] is not None and self.index == 0:
                raise state["mutation_error"]
            return {"done": True}

        async def close(self):
            events.append("wda.close")
            if state.get("close_errors", {}).get(self.index):
                raise state["close_errors"][self.index]

    class AppleRunner:
        def __init__(self, udid, bundle_id, app_path, wda, *, home, bind_host, port):
            assert wda is state["wda"]
            state["runner"] = self
            state["runners"].append(self)
            self.udid, self.bundle_id, self.app_path = udid, bundle_id, app_path
            self.home, self.bind_host, self.port = home, bind_host, port
            events.append("runner.init")

        async def open(self, *, timeout=120):
            self.timeout = timeout
            events.append("runner.open")

        async def close(self):
            events.append("runner.close")

    class Audit:
        set_aside = None

        def __init__(self, path):
            events.append("audit.init")

        def append(self, event, **fields):
            events.append("audit.append")
            state["records"].append({"event": event, **fields})

        def close(self):
            events.append("audit.close")

    async def run_mcp(controller):
        state["controller"] = controller
        events.append("mcp.run")

    monkeypatch.setattr(cli, "DeviceServices", Device)
    monkeypatch.setattr(network, "NetworkBinding", Binding)
    monkeypatch.setattr(network, "BoundWDAClient", WDA)
    monkeypatch.setattr(apple_runner, "AppleWDARunner", AppleRunner)
    monkeypatch.setattr(cli, "AuditLog", Audit)
    monkeypatch.setattr(cli, "run_mcp", run_mcp)
    return events, state


@pytest.mark.asyncio
async def test_explicit_matching_bundle_uses_saved_app_and_selected_tunnel(fakes, tmp_path):
    events, state = fakes
    config = {"udid": UDID, "bundleId": BUNDLE, "appPath": "/signed/build/WebDriverAgentRunner-Runner.app"}
    (tmp_path / "runner.json").write_text(json.dumps(config))

    await cli.run_controller(args(tmp_path))

    runner = state["runner"]
    assert runner is not None
    assert (runner.udid, runner.bundle_id, runner.app_path) == (UDID, BUNDLE, config["appPath"])
    assert (runner.bind_host, runner.port, runner.home) == (HOST, 8123, tmp_path)
    assert 0 < runner.timeout <= 37.0
    assert state["controller"].transport == "paired-network"
    assert state["controller"].media._connection_factory.__self__ is state["binding"]
    assert events.index("binding.for_device") < events.index("wda.open")
    assert events.index("runner.open") < events.index("wda.status")
    assert events.index("mcp.run") < events.index("runner.close")
    assert "forward.close" in events and "wda.close" in events and "device.close" in events


@pytest.mark.asyncio
async def test_audit_lock_failure_starts_no_keeper_and_closes_owned_resources(fakes, tmp_path, monkeypatch):
    events, state = fakes
    (tmp_path / "runner.json").write_text(json.dumps({
        "udid": UDID, "bundleId": BUNDLE, "appPath": "/signed/build/WebDriverAgentRunner-Runner.app",
    }))

    class LockedAudit:
        def __init__(self, path):
            events.append("audit.locked")
            raise RuntimeError("journal writer lock")

    monkeypatch.setattr(cli, "AuditLog", LockedAudit)
    with pytest.raises(RuntimeError, match="journal writer lock"):
        await cli.run_controller(args(tmp_path, no_keep_awake=False))

    assert state["controller"] is None
    assert "audit.locked" in events
    assert state["nudges"] == [] and "wda.locked" not in events
    assert events.index("wda.close") < events.index("device.close")
    assert events.index("device.close") < events.index("forward.close")
    assert events.index("forward.close") < events.index("runner.close")


def saved_runner(home):
    (home / "runner.json").write_text(json.dumps({
        "udid": UDID, "bundleId": BUNDLE, "appPath": "/signed/build/WebDriverAgentRunner-Runner.app",
    }))


@pytest.fixture
def usb_fakes(fakes, monkeypatch):
    import iphone_agent.runner as rootless
    events, state = fakes
    bound_wda = network.BoundWDAClient

    class USBWDA(bound_wda):
        def __init__(self, origin):
            assert origin == "http://127.0.0.1:4321"
            self.base_url = origin
            super().__init__(None)

    class USBForward:
        port = 4321

        def __init__(self, udid, port):
            assert (udid, port) == (UDID, 8123)
            events.append("usb-forward.init")

        async def open(self):
            events.append("forward.open")

        async def close(self):
            events.append("forward.close")

    class RootlessRunner:
        def __init__(self, udid, bundle, port):
            assert (udid, bundle, port) == (UDID, BUNDLE, 8123)
            events.append("rootless.init")

        async def open(self, *, timeout):
            events.append("rootless.open")

        async def close(self):
            events.append("rootless.close")

    monkeypatch.setattr(cli, "USBForward", USBForward)
    monkeypatch.setattr(cli, "WDAClient", USBWDA)
    monkeypatch.setattr(rootless, "WDARunner", RootlessRunner)
    return events, state


@pytest.mark.parametrize("explicit_bundle", [True, False])
async def test_usb_saved_product_selects_apple_before_launch_and_retains_usb_services(usb_fakes, tmp_path, explicit_bundle):
    from iphone_agent.transport import USBForward
    events, state = usb_fakes
    saved_runner(tmp_path)
    options = args(tmp_path)
    options.paired_network = False
    options.runner_bundle_id = BUNDLE if explicit_bundle else None
    await cli.run_controller(options)
    runner = state["runner"]
    assert (runner.udid, runner.bundle_id, runner.bind_host, runner.port) == (UDID, BUNDLE, "127.0.0.1", 8123)
    assert state["controller"].transport == "USB"
    assert state["controller"].media._connection_factory is USBForward
    assert state["wda"].binding is None and state["wda"].base_url == "http://127.0.0.1:4321"
    assert "rootless.init" not in events and "binding.for_device" not in events
    assert events.index("device.info") < events.index("forward.open") < events.index("wda.open") < events.index("runner.open")
    assert "runner.close" in events and "forward.close" in events and "device.close" in events


def fallback_args(home):
    options = args(home)
    options.paired_network, options.network_fallback = False, True
    return options


def watch_runner_launches(monkeypatch, error=None):
    """Returns the launches and waits, in the order they happened. Each launch fails with ``error``, if given."""
    steps = []
    base = apple_runner.AppleWDARunner
    real_sleep = asyncio.sleep

    class Failing(base):
        async def open(self, *, timeout=120):
            steps.append("launch")
            if error is not None:
                raise error

    async def recording_sleep(delay, *rest):
        if delay > 0:
            steps.append(delay)
        await real_sleep(delay, *rest)

    monkeypatch.setattr(apple_runner, "AppleWDARunner", Failing)
    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    return steps


async def test_normal_startup_keeps_its_pace_between_runner_launches(fakes, tmp_path, monkeypatch):
    saved_runner(tmp_path)
    monkeypatch.setattr(cli, "STARTUP_POLL_INTERVAL", 0.01)
    options = args(tmp_path)
    options.startup_timeout = 0.2
    steps = watch_runner_launches(monkeypatch, network.RecoverableNetworkError("the link is away"))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(cli.run_controller(options), 10)
    assert steps[:6] == ["launch", 0.01, "launch", 0.01, "launch", 0.01]


async def test_network_fallback_uses_the_cable_then_the_paired_network_after_unplug(usb_fakes, tmp_path, monkeypatch):
    events, state = usb_fakes
    saved_runner(tmp_path)
    state["usb"] = [True, False]

    async def exercise(host):
        result = await host.execute("reconnect")
        assert result["status"] == "ready" and result["generation"] == 2

    monkeypatch.setattr(cli, "run_mcp", exercise)
    await cli.run_controller(fallback_args(tmp_path))
    usb_runner, network_runner = state["runners"]
    assert usb_runner.bind_host == "127.0.0.1" and network_runner.bind_host == HOST
    assert events.count("device.usb_present") == 2 and events.count("binding.for_device") == 1
    assert events.index("usb-forward.init") < events.index("binding.for_device")


@pytest.mark.parametrize("mode, error, switches", [
    ("fallback", "connection", True),
    ("fallback", "wda", False),  # The phone answered; the link is fine.
    ("usb", "connection", False),  # USB-only mode has no other link to move to.
])
async def test_network_fallback_moves_to_the_paired_network_when_the_usb_link_drops(
        usb_fakes, tmp_path, monkeypatch, mode, error, switches):
    import aiohttp
    events, state = usb_fakes
    saved_runner(tmp_path)
    state["usb"] = [True, False]
    state["mutation_error_before_effect"] = (aiohttp.ServerDisconnectedError() if error == "connection"
                                             else WDAError("POST", "/wda/tap", 500, "unknown error", "bad tap"))

    async def exercise(host):
        first = host.controller
        with pytest.raises(type(state["mutation_error_before_effect"])):
            await host.execute("tap", {"x": 1, "y": 2})
        recovery = host._recovery_task
        assert (recovery is not None) == switches
        if switches:
            await recovery
            assert first.transport == "USB" and host.controller.transport == "paired-network"
            await host.execute("tap", {"x": 3, "y": 4})
            assert state["effects"] == [(1, "tap", {"x": 3, "y": 4})]

    monkeypatch.setattr(cli, "run_mcp", exercise)
    options = fallback_args(tmp_path)
    if mode == "usb":
        options.network_fallback = False
    await cli.run_controller(options)
    assert len(state["runners"]) == (2 if switches else 1)


def hold_wda_wait(monkeypatch, events, *, until=None):
    """The USB runner's wait for WDA does not end by itself, unless ``until`` lets it finish."""
    from iphone_agent import apple_runner
    runner_type = apple_runner.AppleWDARunner

    class Waiting(runner_type):
        async def open(self, *, timeout=120):
            await super().open(timeout=timeout)
            if self.bind_host != "127.0.0.1":
                return
            try:
                await (until or asyncio.Event()).wait()
            except asyncio.CancelledError:
                events.append("usb-wait.stopped")
                raise

    monkeypatch.setattr(apple_runner, "AppleWDARunner", Waiting)
    monkeypatch.setattr(cli, "USB_RECHECK_INTERVAL", 0.001)
    monkeypatch.setattr(cli, "STARTUP_POLL_INTERVAL", 0.001)


def usb_readings(monkeypatch, readings, then):
    """Each usb_present call takes the next reading: a bool, an exception to raise, or "hang"."""
    used = []

    async def usb_present(self):
        reading = readings.pop(0) if readings else then
        used.append(reading)
        if reading == "hang":
            await asyncio.Event().wait()
        if isinstance(reading, Exception):
            raise reading
        return reading

    monkeypatch.setattr(cli.DeviceServices, "usb_present", usb_present)
    return used


async def test_usb_lost_during_the_wda_wait_moves_to_the_network_without_spending_the_budget(
        usb_fakes, tmp_path, monkeypatch, caplog):
    events, state = usb_fakes
    saved_runner(tmp_path)
    hold_wda_wait(monkeypatch, events)
    usb_readings(monkeypatch, [True, True, False], False)
    options = fallback_args(tmp_path)
    await asyncio.wait_for(cli.run_controller(options), 10)
    usb_runner, network_runner = state["runners"]
    assert usb_runner.bind_host == "127.0.0.1" and network_runner.bind_host == HOST
    assert events.count("usb-wait.stopped") == 1 and events.index("usb-wait.stopped") < events.index("binding.for_device")
    assert events.index("runner.close") < events.index("binding.for_device")
    assert network_runner.timeout > usb_runner.timeout - 5
    assert state["controller"].transport == "paired-network"
    assert "the USB link disappeared while waiting for WDA" in caplog.text


async def test_usb_back_after_a_brief_drop_starts_again_on_usb(usb_fakes, tmp_path, monkeypatch):
    events, state = usb_fakes
    saved_runner(tmp_path)
    ready = asyncio.Event()
    hold_wda_wait(monkeypatch, events, until=ready)

    def second_attempt_opens():
        return len(state["runners"]) == 2

    async def release():
        while not second_attempt_opens():
            await asyncio.sleep(0.001)
        ready.set()

    usb_readings(monkeypatch, [True, False, False, True], True)
    helper = asyncio.create_task(release())
    await asyncio.wait_for(cli.run_controller(fallback_args(tmp_path)), 10)
    await asyncio.wait_for(helper, 10)
    assert [runner.bind_host for runner in state["runners"]] == ["127.0.0.1", "127.0.0.1"]
    assert state["controller"].transport == "USB" and "binding.for_device" not in events


async def test_one_not_listed_reading_does_not_end_the_attempt(usb_fakes, tmp_path, monkeypatch, caplog):
    # usbmuxd listed nothing for a moment with the phone attached (usb-poll.out, 21:46:18).
    events, state = usb_fakes
    saved_runner(tmp_path)
    ready = asyncio.Event()
    hold_wda_wait(monkeypatch, events, until=ready)
    used = usb_readings(monkeypatch, [True, False, True, False, True], True)

    async def release():
        while len(used) < 7:
            await asyncio.sleep(0.001)
        ready.set()

    helper = asyncio.create_task(release())
    await asyncio.wait_for(cli.run_controller(fallback_args(tmp_path)), 10)
    await asyncio.wait_for(helper, 10)
    assert len(state["runners"]) == 1 and "usb-wait.stopped" not in events
    assert state["controller"].transport == "USB"
    assert "not listed on USB (1 in a row)" in caplog.text


@pytest.mark.parametrize("second", [False, ConnectionRefusedError("usbmuxd socket")], ids=["not-listed", "failed"])
@pytest.mark.parametrize("first", [False, ConnectionRefusedError("usbmuxd socket")], ids=["not-listed", "failed"])
async def test_two_readings_without_the_cable_in_a_row_end_the_attempt(usb_fakes, tmp_path, monkeypatch, caplog, first, second):
    events, state = usb_fakes
    saved_runner(tmp_path)
    hold_wda_wait(monkeypatch, events)
    # The new attempt's own choice takes one reading, as at every attempt start.
    usb_readings(monkeypatch, [True, True, first, second, False], False)
    await asyncio.wait_for(cli.run_controller(fallback_args(tmp_path)), 10)
    assert [runner.bind_host for runner in state["runners"]] == ["127.0.0.1", HOST]
    assert events.count("usb-wait.stopped") == 1
    assert state["controller"].transport == "paired-network"
    reason = "the USB link disappeared" if second is False else "USB presence could not be read"
    assert f"{reason} while waiting for WDA" in caplog.text


async def test_startup_error_stays_first_and_its_cleanup_failure_is_kept(usb_fakes, tmp_path, monkeypatch):
    saved_runner(tmp_path)
    failure = RuntimeError("the real cause")

    async def fail(self, **kwargs):
        raise failure

    async def close_fails(self):
        raise OSError("xcodebuild would not stop")

    monkeypatch.setattr(apple_runner.AppleWDARunner, "open", fail)
    monkeypatch.setattr(apple_runner.AppleWDARunner, "close", close_fails)
    with pytest.raises(BaseExceptionGroup, match="Connection startup and cleanup failed") as caught:
        await cli.run_controller(fallback_args(tmp_path))
    assert caught.value.exceptions[0] is failure
    assert isinstance(caught.value.cleanup_failure, BaseExceptionGroup)
    assert "xcodebuild would not stop" in "".join(caught.value.__notes__)


async def test_serving_error_stays_the_error_when_cleanup_fails_too(usb_fakes, tmp_path, monkeypatch):
    saved_runner(tmp_path)

    async def close_fails(self):
        raise OSError("xcodebuild would not stop")

    async def serving_fails(host):
        raise RuntimeError("serving broke")

    monkeypatch.setattr(apple_runner.AppleWDARunner, "close", close_fails)
    usb_readings(monkeypatch, [], True)
    options = fallback_args(tmp_path)
    options.no_keep_awake = True
    options.command = "mcp"
    with pytest.raises(RuntimeError, match="serving broke") as caught:
        await cli.run_controller(options, hold=serving_fails)
    assert "xcodebuild would not stop" in "".join(caught.value.__notes__)
    assert isinstance(caught.value.cleanup_failure, BaseExceptionGroup)


async def test_cleanup_failure_with_no_earlier_error_is_still_raised(usb_fakes, tmp_path, monkeypatch):
    saved_runner(tmp_path)

    async def close_fails(self):
        raise OSError("xcodebuild would not stop")

    async def serving_ends(host):
        pass

    monkeypatch.setattr(apple_runner.AppleWDARunner, "close", close_fails)
    usb_readings(monkeypatch, [], True)
    options = fallback_args(tmp_path)
    options.no_keep_awake = True
    options.command = "mcp"
    with pytest.raises(BaseExceptionGroup):
        await cli.run_controller(options, hold=serving_ends)


@pytest.mark.parametrize("bad", [ConnectionRefusedError("usbmuxd socket"), "hang"], ids=["fails", "hangs"])
async def test_one_bad_presence_reading_does_not_end_the_attempt(usb_fakes, tmp_path, monkeypatch, caplog, bad):
    events, state = usb_fakes
    saved_runner(tmp_path)
    ready = asyncio.Event()
    hold_wda_wait(monkeypatch, events, until=ready)
    monkeypatch.setattr(cli, "USB_RECHECK_TIMEOUT", 0.01)
    # Bad readings apart: only consecutive ones count.
    used = usb_readings(monkeypatch, [True, bad, True, bad, True], True)

    async def release():
        while len(used) < 7:
            await asyncio.sleep(0.001)
        ready.set()

    helper = asyncio.create_task(release())
    await asyncio.wait_for(cli.run_controller(fallback_args(tmp_path)), 10)
    await asyncio.wait_for(helper, 10)
    assert len(state["runners"]) == 1 and "usb-wait.stopped" not in events
    assert state["controller"].transport == "USB"
    assert "USB presence check failed (1 in a row)" in caplog.text


@pytest.mark.parametrize("bad", [ConnectionRefusedError("usbmuxd socket"), "hang"], ids=["fails", "hangs"])
async def test_a_presence_check_that_keeps_failing_counts_as_no_cable(usb_fakes, tmp_path, monkeypatch, caplog, bad):
    events, state = usb_fakes
    saved_runner(tmp_path)
    hold_wda_wait(monkeypatch, events)
    monkeypatch.setattr(cli, "USB_RECHECK_TIMEOUT", 0.01)
    # The next attempt's own choice follows the _usb_present rule: an unreadable list means no cable.
    usb_readings(monkeypatch, [True, bad, bad], ConnectionRefusedError("usbmuxd socket"))
    await asyncio.wait_for(cli.run_controller(fallback_args(tmp_path)), 10)
    assert [runner.bind_host for runner in state["runners"]] == ["127.0.0.1", HOST]
    assert state["controller"].transport == "paired-network"
    assert "USB presence could not be read while waiting for WDA" in caplog.text


@pytest.mark.parametrize("mode", ["usb", "paired-network"])
async def test_other_modes_do_not_recheck_usb_during_the_wda_wait(usb_fakes, tmp_path, monkeypatch, mode):
    from iphone_agent import apple_runner
    events, state = usb_fakes
    saved_runner(tmp_path)
    runner_type = apple_runner.AppleWDARunner

    class Waiting(runner_type):
        async def open(self, *, timeout=120):
            await super().open(timeout=timeout)
            await asyncio.sleep(0.05)

    monkeypatch.setattr(apple_runner, "AppleWDARunner", Waiting)
    monkeypatch.setattr(cli, "USB_RECHECK_INTERVAL", 0.001)
    used = usb_readings(monkeypatch, [], False)
    options = args(tmp_path)
    options.paired_network = mode == "paired-network"
    await asyncio.wait_for(cli.run_controller(options), 10)
    assert not used and len(state["runners"]) == 1
    assert state["controller"].transport == mode.replace("usb", "USB")


async def test_fallback_attempt_already_on_the_network_does_not_watch_the_cable(usb_fakes, tmp_path, monkeypatch):
    from iphone_agent import apple_runner
    events, state = usb_fakes
    saved_runner(tmp_path)
    runner_type = apple_runner.AppleWDARunner

    class Waiting(runner_type):
        async def open(self, *, timeout=120):
            await super().open(timeout=timeout)
            await asyncio.sleep(0.05)

    monkeypatch.setattr(apple_runner, "AppleWDARunner", Waiting)
    monkeypatch.setattr(cli, "USB_RECHECK_INTERVAL", 0.001)
    used = usb_readings(monkeypatch, [], False)
    await asyncio.wait_for(cli.run_controller(fallback_args(tmp_path)), 10)
    assert used == [False] and len(state["runners"]) == 1  # Only the choice of link read it.
    assert state["controller"].transport == "paired-network"


async def test_runner_failure_during_the_usb_wait_is_reported_as_itself(usb_fakes, tmp_path, monkeypatch):
    from iphone_agent import apple_runner
    events, state = usb_fakes
    saved_runner(tmp_path)
    runner_type = apple_runner.AppleWDARunner

    class Failing(runner_type):
        async def open(self, *, timeout=120):
            raise apple_runner.AppleRunnerError("xcodebuild exited")

    monkeypatch.setattr(apple_runner, "AppleWDARunner", Failing)
    usb_readings(monkeypatch, [], True)
    with pytest.raises(apple_runner.AppleRunnerError, match="xcodebuild exited"):
        await asyncio.wait_for(cli.run_controller(fallback_args(tmp_path)), 10)
    assert "runner.close" in events


async def test_startup_deadline_during_the_usb_wait_is_still_the_startup_timeout(usb_fakes, tmp_path, monkeypatch):
    events, state = usb_fakes
    saved_runner(tmp_path)
    hold_wda_wait(monkeypatch, events)
    usb_readings(monkeypatch, [], True)
    with pytest.raises(TimeoutError, match="within 37s"):
        await asyncio.wait_for(cli.run_controller(fallback_args(tmp_path), first_connect_timeout=0.05), 10)
    assert events.count("usb-wait.stopped") == 1 and "runner.close" in events


async def test_forwarder_error_storm_leaves_few_log_lines_and_other_errors_visible(monkeypatch, caplog):
    from pymobiledevice3.exceptions import ConnectionFailedError
    from iphone_agent import transport

    monkeypatch.setattr(transport.FORWARDER_LOG_LIMIT, "seen", {})
    clock = [100.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    forward = transport.USBForward(UDID, 8100)

    async def refused():
        raise ConnectionFailedError()

    class Writer:
        def get_extra_info(self, name):
            return ("127.0.0.1", 1)

        def close(self):
            pass

        async def wait_closed(self):
            pass

    monkeypatch.setattr(forward.forwarder, "_establish_remote_connection", refused)
    for _ in range(706):
        await forward.forwarder._handle_server_connection(None, Writer())
        clock[0] += 0.25  # Three minutes at the rate the device run logged.
    storm = [record for record in caplog.records if "failed to connect" in record.getMessage()]
    assert 2 <= len(storm) <= 4  # The first one at once, then one a minute.
    assert "repeats dropped" in storm[1].getMessage()
    forward.forwarder.logger.error("a different failure")
    assert "a different failure" in caplog.text


async def test_first_connect_budget_applies_once_and_reconnects_get_the_full_timeout(usb_fakes, tmp_path, monkeypatch):
    events, state = usb_fakes
    saved_runner(tmp_path)

    async def exercise(host):
        assert (await host.execute("reconnect"))["generation"] == 2

    monkeypatch.setattr(cli, "run_mcp", exercise)
    options = fallback_args(tmp_path)
    await cli.run_controller(options, first_connect_timeout=5.0)
    first, second = state["runners"]
    assert first.timeout <= 5.0
    assert second.timeout > 30  # These arguments ask for 37 s.
    assert options.startup_timeout == 37.0


@pytest.mark.parametrize("reading", [False, ConnectionRefusedError("usbmuxd socket")], ids=["not-listed", "unreadable"])
async def test_network_fallback_without_a_readable_cable_starts_on_the_paired_network(usb_fakes, tmp_path, monkeypatch, reading):
    events, state = usb_fakes
    saved_runner(tmp_path)
    usb_readings(monkeypatch, [], reading)
    await cli.run_controller(fallback_args(tmp_path))
    assert state["controller"].transport == "paired-network"
    assert state["runner"].bind_host == HOST and "usb-forward.init" not in events


async def test_network_fallback_needs_the_saved_signed_product(usb_fakes, tmp_path):
    events, _ = usb_fakes
    with pytest.raises(RuntimeError, match="needs the signed appPath"):
        await cli.run_controller(fallback_args(tmp_path))
    assert events == []


@pytest.mark.parametrize("paired_network", [False, True], ids=["usb", "paired-network"])
@pytest.mark.parametrize("mismatch, message", [("bundleId", "different runner bundle"), ("udid", "different iPhone")])
async def test_saved_product_mismatch_fails_before_device_or_launch(usb_fakes, tmp_path, mismatch, message, paired_network):
    events, state = usb_fakes
    config = {"udid": UDID, "bundleId": BUNDLE, "appPath": "/signed/build/WebDriverAgentRunner-Runner.app"}
    config[mismatch] = "OTHER"
    (tmp_path / "runner.json").write_text(json.dumps(config))
    options = args(tmp_path)
    options.paired_network = paired_network
    with pytest.raises(RuntimeError, match=message):
        await cli.run_controller(options)
    assert events == [] and state["runner"] is None


async def test_usb_explicit_installed_runner_without_product_uses_only_rootless(usb_fakes, tmp_path):
    events, state = usb_fakes
    options = args(tmp_path)
    options.paired_network = False
    await cli.run_controller(options)
    assert "rootless.open" in events and "rootless.close" in events
    assert state["runner"] is None


@pytest.mark.parametrize("failure", [TimeoutError("nested XCTest transport timeout"), RuntimeError("invalid signed product")])
async def test_usb_apple_failure_is_preserved_without_rootless_fallback(usb_fakes, tmp_path, monkeypatch, failure):
    events, _ = usb_fakes
    saved_runner(tmp_path)

    async def fail(self, **kwargs):
        events.append("runner.open.failed")
        raise failure

    monkeypatch.setattr(apple_runner.AppleWDARunner, "open", fail)
    options = args(tmp_path)
    options.paired_network = False
    options.startup_timeout = 600
    with pytest.raises(type(failure)) as caught:
        await cli.run_controller(options)
    assert caught.value is failure
    assert "rootless.init" not in events and "mcp.run" not in events
    assert "runner.close" in events and "forward.close" in events and "wda.close" in events


@pytest.mark.parametrize("cancel", [True, False])
async def test_usb_apple_startup_cancel_or_deadline_closes_without_fallback(usb_fakes, tmp_path, monkeypatch, cancel):
    events, _ = usb_fakes
    saved_runner(tmp_path)
    started = asyncio.Event()

    async def wait(self, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(apple_runner.AppleWDARunner, "open", wait)
    options = args(tmp_path)
    options.paired_network = False
    options.startup_timeout = 600 if cancel else 0.05
    task = asyncio.create_task(cli.run_controller(options))
    await started.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError) as caught:
        await task
    if not cancel:
        assert "within 0.05s" in str(caught.value)
    assert "rootless.init" not in events and "mcp.run" not in events
    assert "runner.close" in events and "forward.close" in events and "wda.close" in events


@pytest.mark.parametrize("failure", ["ip changed", "response lost"])
async def test_managed_recovery_replaces_bound_session_after_error_receipt_without_replay(fakes, tmp_path, monkeypatch, failure):
    events, state = fakes
    saved_runner(tmp_path)
    state["hosts"] = [HOST, "fd00:db8:1::2"]
    if failure == "ip changed":
        error = state["mutation_error_before_effect"] = network.NetworkAddressChangedError("selected phone moved")
        initial_effects = 0
    else:
        error = state["mutation_error"] = network.WDATransportError("action response lost")
        initial_effects = 1

    async def exercise(host):
        first = host.controller
        with pytest.raises(type(error)):
            await host.execute("tap", {"x": 1, "y": 2})
        recovery = host._recovery_task
        assert recovery is not None
        await recovery
        assert host.controller is not first
        assert first.wda.binding.host == HOST
        assert host.wda.binding.host == "fd00:db8:1::2"
        assert len(state["effects"]) == initial_effects
        requests = [r for r in state["records"] if r["event"] == "request"]
        assert [r["action"] for r in requests] == ["tap", "reconnect"]
        first_result = next(i for i, r in enumerate(state["records"])
                            if r["event"] == "result" and r["request_id"] == requests[0]["request_id"])
        recovery_request = next(i for i, r in enumerate(state["records"])
                                if r["event"] == "request" and r["action"] == "reconnect")
        assert state["records"][first_result]["status"] == "error" and first_result < recovery_request
        assert events.index("runner.close") < events.index("runner.init", events.index("runner.init") + 1)
        await host.execute("tap", {"x": 3, "y": 4})
        assert len(state["effects"]) == initial_effects + 1

    monkeypatch.setattr(cli, "run_mcp", exercise)
    await cli.run_controller(args(tmp_path))
    assert len(state["runners"]) == 2


async def test_explicit_reconnect_reports_unknown_old_cleanup_and_retries_it_on_close(fakes, tmp_path, monkeypatch):
    events, state = fakes
    saved_runner(tmp_path)
    state["close_errors"] = {0: network.NetworkAddressChangedError("old session cleanup unavailable")}

    async def exercise(host):
        result = await host.execute("reconnect")
        assert result["status"] == "ready" and result["generation"] == 2
        assert result["previousCleanupErrors"] and result["previousCleanupErrors"][0]["outcome"] == "unknown"
        assert any(record.get("outcome") == "unknown" for record in state["records"])
        state["close_errors"].clear()

    monkeypatch.setattr(cli, "run_mcp", exercise)
    await cli.run_controller(args(tmp_path))
    assert events.count("wda.close") == 3


async def test_cold_start_waits_for_exact_paired_device_ip_within_one_deadline(fakes, tmp_path, monkeypatch):
    events, state = fakes
    saved_runner(tmp_path)
    state["resolve_errors"] = [network.CoreDeviceUnavailableError("no IP yet"), network.CoreDeviceTimeoutError("read timed out")]
    monkeypatch.setattr(cli, "STARTUP_POLL_INTERVAL", 0.001)
    await cli.run_controller(args(tmp_path))
    assert events.count("binding.for_device") == 3
    assert len(state["wdas"]) == len(state["runners"]) == 1


@pytest.mark.parametrize("failure", ["wrong UDID", "unpaired", "malformed"])
async def test_cold_start_identity_failures_never_retry_or_launch(fakes, tmp_path, failure):
    events, state = fakes
    saved_runner(tmp_path)
    state["resolve_error"] = network.NetworkBindingError(failure)
    with pytest.raises(network.NetworkBindingError, match=failure):
        await cli.run_controller(args(tmp_path))
    assert events == ["binding.for_device"] and not state["runners"]


async def test_cold_start_missing_ip_expires_and_cancel_never_restarts(fakes, tmp_path, monkeypatch):
    events, state = fakes
    saved_runner(tmp_path)
    state["resolve_error"] = network.CoreDeviceUnavailableError("no IP yet")
    monkeypatch.setattr(cli, "STARTUP_POLL_INTERVAL", 0.001)
    options = args(tmp_path)
    options.startup_timeout = 0.01
    with pytest.raises(TimeoutError, match="within 0.01s"):
        await cli.run_controller(options)
    assert not state["wdas"] and not state["runners"]
    state["resolve_error"] = None
    ready = asyncio.Event()

    async def wait_for_client(host):
        ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(cli, "run_mcp", wait_for_client)
    task = asyncio.create_task(cli.run_controller(args(tmp_path)))
    await ready.wait()
    count = events.count("binding.for_device")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events.count("binding.for_device") == count
    assert events[-1] == "audit.close" and "runner.close" in events


async def test_cancel_during_failed_startup_cleanup_finishes_owned_close_and_never_retries(fakes, tmp_path, monkeypatch):
    events, state = fakes
    saved_runner(tmp_path)
    closing, release = asyncio.Event(), asyncio.Event()
    wda_type = network.BoundWDAClient

    async def failed_status(self, method, path, body=None):
        raise network.CoreDeviceUnavailableError("transport disappeared during startup")

    async def owned_close(self):
        events.append("wda.close.started")
        closing.set()
        await release.wait()
        events.append("wda.close.finished")

    monkeypatch.setattr(wda_type, "call", failed_status)
    monkeypatch.setattr(wda_type, "close", owned_close)
    task = asyncio.create_task(cli.run_controller(args(tmp_path)))
    await closing.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and events.count("binding.for_device") == 1
    release.set()
    with pytest.raises(BaseExceptionGroup) as caught:
        await task

    def leaves(error):
        return [leaf for child in error.exceptions for leaf in leaves(child)] if isinstance(error, BaseExceptionGroup) else [error]

    assert any(isinstance(error, asyncio.CancelledError) for error in leaves(caught.value))
    assert events.count("binding.for_device") == 1
    assert "wda.close.finished" in events and "forward.close" in events and "runner.close" in events


async def test_owned_cleanup_bounds_every_resource_including_the_keeper(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONNECTION_CLEANUP_TIMEOUT", 0.05)
    events = []

    def stalled(name):
        async def close():
            events.append(name)
            await asyncio.Event().wait()
        return close

    device, wda = AsyncMock(), AsyncMock()
    device.udid = UDID
    wda.close.side_effect = stalled("wda")
    audit = AuditLog(tmp_path / "cleanup-budgets.jsonl")
    controller = Controller(device, wda, audit)
    controller.keep_awake = SimpleNamespace(close=stalled("keeper"), status={"active": False})
    session = cli._OwnedSession()
    session.controller = controller
    try:
        began = asyncio.get_running_loop().time()
        errors = await session.close()
        assert asyncio.get_running_loop().time() - began < 1.0
        assert events == ["keeper", "wda"]
        assert len(errors) == 1 and [type(error) for error in errors[0].exceptions] == [TimeoutError, TimeoutError]
        device.close.assert_awaited_once()
        assert all(record["event"] != "keep_awake" for record in AuditLog.verify(audit.path))  # It did not stop.
        assert audit.fd is not None  # The host still owns the shared audit.
    finally:
        audit.close()


@pytest.mark.parametrize("offline", [True, False])
async def test_real_bound_wda_offline_probe_launches_runner_but_identity_failure_does_not(tmp_path, monkeypatch, offline):
    import aiohttp
    payload = {"result": {"hardwareProperties": {"udid": UDID if offline else "OTHER"},
                           "connectionProperties": {"pairingState": "paired", "tunnelState": "connected", "tunnelIPAddress": HOST}}}
    spawned, requests = [], []
    finished = asyncio.Event()

    async def details(*arguments):
        return payload

    async def request(self, method, path, body=None):
        requests.append((method, path))
        if not spawned:
            raise aiohttp.ClientConnectionError("WDA is not running")
        return {"ready": True, "state": "success", "build": {"productBundleIdentifier": BUNDLE.removesuffix(".xctrunner")}}

    class Process:
        pid = 9876
        returncode = None

        async def wait(self):
            await finished.wait()
            self.returncode = -15
            return -15

    process = Process()

    async def spawn(*command, **kwargs):
        assert command[:2] == ("xcodebuild", "test-without-building")
        spawned.append(command)
        return process

    prepared = []

    async def developer_image(udid, timeout):
        assert not spawned  # Mounted before xcodebuild launches.
        prepared.append(udid)

    async def unlocked(udid, timeout):
        assert not spawned and not prepared  # Read first: xcodebuild only waits on a locked phone.
        return False

    monkeypatch.setattr(network, "_coredevice_details", details)
    monkeypatch.setattr(network, "ensure_developer_image", developer_image)
    monkeypatch.setattr(network, "device_passcode_required", unlocked)
    monkeypatch.setattr(network.WDAClient, "call", request)
    monkeypatch.setattr(apple_runner.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(apple_runner.os, "killpg", lambda *_arguments: finished.set())
    binding = network.NetworkBinding(UDID, f"http://[{HOST}]:8123")
    wda = network.BoundWDAClient(binding)
    runner = apple_runner.AppleWDARunner(UDID, BUNDLE, tmp_path / "Debug-iphoneos/WebDriverAgentRunner-Runner.app",
                                       wda, home=tmp_path)
    plan = tmp_path / "selected.xctestrun"
    plan.write_bytes(b"plan")
    monkeypatch.setattr(runner, "_validate_product", lambda: plan)
    monkeypatch.setattr(runner, "_verify_signature", AsyncMock())
    monkeypatch.setattr(runner, "_write_launch_plan", lambda selected: plan)
    try:
        if offline:
            await runner.open(timeout=1)
            assert len(spawned) == 1 and requests == [("GET", "/status"), ("GET", "/status")]
            assert not runner.adopted and prepared == [UDID]
        else:
            with pytest.raises(network.NetworkBindingError, match="UDID"):
                await runner.open(timeout=1)
            assert spawned == requests == prepared == []
    finally:
        await runner.close()
        await wda.close()


async def test_deadline_during_an_attempt_reports_the_last_wait_reason(fakes, tmp_path, monkeypatch):
    events, state = fakes
    saved_runner(tmp_path)
    state["resolve_errors"] = [network.DeveloperImageLockedError("The iPhone is locked; unlock the phone")]
    monkeypatch.setattr(cli, "STARTUP_POLL_INTERVAL", 0.001)

    async def hang(self, *, timeout=120):
        await asyncio.sleep(3600)

    monkeypatch.setattr(apple_runner.AppleWDARunner, "open", hang)
    options = args(tmp_path)
    options.startup_timeout = 0.2
    with pytest.raises(TimeoutError, match="within 0.2s") as raised:
        await cli.run_controller(options)
    assert isinstance(raised.value.__cause__, network.DeveloperImageLockedError)


@pytest.mark.parametrize("locked_error", ["DeveloperImageLockedError", "DeviceLockedError"])
async def test_phone_still_locked_after_the_grace_ends_the_connect_with_what_the_owner_must_do(
        fakes, tmp_path, monkeypatch, locked_error):
    events, state = fakes
    saved_runner(tmp_path)
    state["resolve_error"] = getattr(network, locked_error)("The iPhone is locked; unlock the phone")
    monkeypatch.setattr(cli, "STARTUP_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(cli, "LOCKED_CONNECT_GRACE", 0.1)
    started = asyncio.get_running_loop().time()
    with pytest.raises(PhoneLocked, match="The iPhone is locked.*Unlock it.*then retry the call") as raised:
        await cli.run_controller(args(tmp_path))  # Startup timeout is 37 s; the answer comes long before it.
    assert asyncio.get_running_loop().time() - started < 5
    assert isinstance(raised.value.__cause__, getattr(network, locked_error))
    assert state["runners"] == [] and "runner.open" not in events


async def test_lock_query_failing_between_locked_readings_does_not_restart_the_grace(fakes, tmp_path, monkeypatch):
    """A locked phone asleep on Wi-Fi answers some lock queries and fails others."""
    events, state = fakes
    saved_runner(tmp_path)
    state["resolve_errors"] = [network.DeviceLockedError("The iPhone is locked; unlock the phone"),
                               network.CoreDeviceUnavailableError("lockState query failed with exit status 1")] * 60
    monkeypatch.setattr(cli, "STARTUP_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(cli, "LOCKED_CONNECT_GRACE", 0.1)

    async def hold(host):
        events.append("held")

    with pytest.raises(PhoneLocked, match="The iPhone is locked"):
        await cli.run_controller(args(tmp_path), hold=hold)
    assert "held" not in events and state["resolve_errors"]  # Ended long before the errors ran out.


async def test_phone_unlocked_within_the_grace_connects(fakes, tmp_path, monkeypatch):
    events, state = fakes
    saved_runner(tmp_path)
    state["resolve_errors"] = [network.DeveloperImageLockedError("The iPhone is locked; unlock the phone")] * 3
    monkeypatch.setattr(cli, "STARTUP_POLL_INTERVAL", 0.001)

    async def hold(host):
        events.append("held")

    await cli.run_controller(args(tmp_path), hold=hold)
    assert "held" in events


# Keep-awake: one input-nudge keeper per connection, started without touching the screen.

NUDGE = (0x07, 0x68, 0.02)


async def test_connect_starts_the_keeper_by_default_and_audits_its_start_and_stop(fakes, tmp_path, monkeypatch):
    events, state = fakes
    saved_runner(tmp_path)
    seen = {}

    async def exercise(host):
        keeper = seen["keeper"] = host.controller.keep_awake
        assert isinstance(keeper, keepawake.ActivityKeepAwake)
        assert keeper.idle_limit == keepawake.DEFAULT_IDLE_LIMIT and keeper.wda is state["wda"]
        assert keeper.status["active"] is True and keeper.status["lastError"] is None
        assert state["nudges"] == [(0, *NUDGE)]  # The first nudge went out before the client got the host.
        assert (await host.execute("capabilities"))["keepAwake"]["method"] == "input-nudge"

    monkeypatch.setattr(cli, "run_mcp", exercise)
    await cli.run_controller(args(tmp_path, no_keep_awake=False))
    assert seen["keeper"]._task is None and seen["keeper"].status["active"] is False
    operations = [record["operation"] for record in state["records"] if record["event"] == "keep_awake"]
    assert operations == ["start", "stop"]
    start = next(record for record in state["records"] if record["event"] == "keep_awake")
    assert start["status"]["changesSettings"] is False and start["device"] == UDID


async def test_idle_timeout_reaches_the_keeper(fakes, tmp_path, monkeypatch):
    _, state = fakes
    saved_runner(tmp_path)
    options = args(tmp_path, no_keep_awake=False)
    options.idle_timeout = 60.0
    await cli.run_controller(options)
    assert state["controller"].keep_awake.idle_limit == 60.0


async def test_no_keep_awake_starts_no_keeper_and_sends_nothing(fakes, tmp_path):
    events, state = fakes
    saved_runner(tmp_path)
    await cli.run_controller(args(tmp_path, no_keep_awake=True))
    assert state["controller"].keep_awake is None
    assert state["nudges"] == [] and "wda.locked" not in events
    assert not any(record["event"] == "keep_awake" for record in state["records"])


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
async def test_connect_never_navigates_the_phone_or_waits_for_an_unlock(fakes, tmp_path, monkeypatch, locked):
    """The old Settings routine opened Settings with deep_link, tap and tap_element on every connect."""
    events, state = fakes
    saved_runner(tmp_path)
    state["locks"] = [locked]
    options = args(tmp_path, no_keep_awake=False)
    options.startup_timeout = 5.0

    async def exercise(host):
        assert not any(event.startswith("wda.dispatch.") for event in events)
        assert state["effects"] == []
        assert not any(record["event"] == "request" for record in state["records"])  # No action ran.
        assert host.controller.keep_awake.status["locked"] is locked
        assert state["nudges"] == ([] if locked else [(0, *NUDGE)])

    monkeypatch.setattr(cli, "run_mcp", exercise)
    began = asyncio.get_running_loop().time()
    await asyncio.wait_for(cli.run_controller(options), 10)
    assert asyncio.get_running_loop().time() - began < 2.0
    assert len(state["runners"]) == 1


async def test_failing_nudge_does_not_fail_the_connect(fakes, tmp_path, monkeypatch):
    _, state = fakes
    saved_runner(tmp_path)

    async def refused(self, page, usage, duration):
        raise WDAError("POST", "/wda/performIoHidEvent", 404, "unknown command", "not in this WDA build")

    monkeypatch.setattr(network.BoundWDAClient, "device_event", refused)
    await cli.run_controller(args(tmp_path, no_keep_awake=False))
    status = state["controller"].keep_awake.status
    assert "not in this WDA build" in status["lastError"]
    assert any(record["event"] == "keep_awake" and record["operation"] == "start" for record in state["records"])


async def test_each_connection_gets_its_own_keeper_and_the_replaced_one_stops(fakes, tmp_path, monkeypatch):
    _, state = fakes
    saved_runner(tmp_path)
    seen = []

    async def exercise(host):
        seen.append(host.controller.keep_awake)
        await host.execute("reconnect")
        seen.append(host.controller.keep_awake)
        assert seen[0]._task is None and seen[1]._task is not None and not seen[1]._task.done()
        assert seen[1].wda is state["wdas"][1]
        assert [nudge[0] for nudge in state["nudges"]] == [0, 1]

    monkeypatch.setattr(cli, "run_mcp", exercise)
    await cli.run_controller(args(tmp_path, no_keep_awake=False))
    assert len(seen) == 2 and seen[0] is not seen[1] and seen[1]._task is None


def parsed(monkeypatch, tmp_path, *arguments):
    captured = {}

    async def run(options):
        captured["args"] = options
        return 0

    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr("sys.argv", ["iphone-agent", "--home", str(tmp_path), "serve", *arguments])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    return exited.value.code, captured.get("args")


def test_command_line_keep_awake_options(monkeypatch, tmp_path, capsys):
    code, options = parsed(monkeypatch, tmp_path)
    assert code == 0 and options.no_keep_awake is False and options.idle_timeout == 1800.0
    code, options = parsed(monkeypatch, tmp_path, "--no-keep-awake", "--idle-timeout", "60")
    assert code == 0 and options.no_keep_awake is True and options.idle_timeout == 60.0
    code, options = parsed(monkeypatch, tmp_path, "--resume-keep-awake")
    assert code == 2 and options is None and "unrecognized arguments: --resume-keep-awake" in capsys.readouterr().err


async def test_idle_timeout_zero_keeps_the_phone_awake_without_a_limit(fakes, tmp_path):
    _, state = fakes
    saved_runner(tmp_path)
    options = args(tmp_path, no_keep_awake=False)
    options.idle_timeout = 0
    await cli.run_controller(options)
    assert state["controller"].keep_awake.idle_limit is None


async def test_media_work_keeps_the_keeper_in_use_past_the_idle_limit(fakes, tmp_path, monkeypatch):
    import time
    saved_runner(tmp_path)
    offset = [0.0]
    monkeypatch.setattr(keepawake, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0]))

    async def exercise(host):
        keeper = host.controller.keep_awake
        offset[0] += keeper.idle_limit + 1
        host.controller.media = SimpleNamespace(busy=True, close=AsyncMock())
        assert keeper.in_use
        host.controller.media.busy = False
        assert not keeper.in_use

    monkeypatch.setattr(cli, "run_mcp", exercise)
    await cli.run_controller(args(tmp_path, no_keep_awake=False))


@pytest.mark.asyncio
async def test_phone_held_by_another_controller_refuses_before_any_connection(fakes, tmp_path):
    from iphone_agent import locks
    events, state = fakes
    (tmp_path / "runner.json").write_text(json.dumps({"udid": UDID, "bundleId": BUNDLE, "appPath": "/signed/app"}))
    other = locks.PhoneLock(UDID, mode="serve", home=tmp_path / "other-home")
    await other.acquire(1)
    try:
        with pytest.raises(locks.PhoneBusy, match=r"serve, pid \d+"):
            await cli.run_controller(args(tmp_path))
    finally:
        other.release()
    assert "binding.for_device" not in events and "wda.init" not in events


@pytest.mark.asyncio
async def test_controller_holds_the_phone_through_reconnects_and_frees_it_after_cleanup(fakes, tmp_path, monkeypatch):
    from iphone_agent import locks
    events, state = fakes
    (tmp_path / "runner.json").write_text(json.dumps({"udid": UDID, "bundleId": BUNDLE, "appPath": "/signed/app"}))
    seen = {}

    async def serving(host):
        lock = locks.PhoneLock(UDID, mode="serve", home=tmp_path / "other")
        seen["holder"] = json.loads(lock.path.read_text())
        with pytest.raises(locks.PhoneBusy):
            await lock.acquire(0.1)
        await host.execute("reconnect")  # A reconnect inside the controller must not lock itself out.
        seen["after_reconnect"] = json.loads(lock.path.read_text())

    cleaning = []
    original = cli._OwnedSession.close

    async def close(self):
        lock_path = locks.PhoneLock(UDID, mode="serve", home=tmp_path).path
        cleaning.append(json.loads(lock_path.read_text())["state"])
        return await original(self)

    monkeypatch.setattr(cli._OwnedSession, "close", close)
    await cli.run_controller(args(tmp_path), hold=serving)
    assert seen["holder"]["mode"] == "mcp" and seen["holder"]["pid"] == os.getpid()
    assert seen["holder"]["home"] == str(tmp_path) and seen["holder"]["state"] == "active"
    assert seen["after_reconnect"]["state"] == "active"
    assert cleaning[-1] == "cleaning"
    free = locks.PhoneLock(UDID, mode="serve", home=tmp_path / "other")
    await free.acquire(0.1)
    free.release()


@pytest.mark.asyncio
async def test_replaced_wda_session_is_recorded_in_the_audit_journal(fakes, tmp_path):
    events, state = fakes
    (tmp_path / "runner.json").write_text(json.dumps({"udid": UDID, "bundleId": BUNDLE, "appPath": "/signed/app"}))

    async def serving(host):
        state["wda"].on_session_replaced("OLD-SESSION")

    await cli.run_controller(args(tmp_path), hold=serving)
    replaced = [record for record in state["records"] if record["event"] == "wda_session_replaced"]
    assert replaced == [{"event": "wda_session_replaced", "device": UDID, "session": "OLD-SESSION"}]


@pytest.mark.asyncio
async def test_controller_start_tidies_the_state_directory_into_one_audit_entry(fakes, tmp_path):
    import time
    events, state = fakes
    old = time.time() - 40 * 24 * 3600
    build = tmp_path / "build"
    used, stale = build / "used", build / "stale"
    (used / "App.app").mkdir(parents=True)
    (stale / "Big").mkdir(parents=True)
    (stale / "Big" / "blob").write_bytes(b"x" * 1000)
    for path in (used, stale):
        os.utime(path, (old, old))
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    expired = recordings / "recording-old.mp4"
    expired.write_bytes(b"v" * 300)
    os.utime(expired, (old, old))
    (tmp_path / "runner.json").write_text(json.dumps({"udid": UDID, "bundleId": BUNDLE,
                                                       "appPath": str(used / "App.app")}))
    options = args(tmp_path)
    options.keep_recordings_days = 30

    await cli.run_controller(options)
    assert used.exists() and not stale.exists() and not expired.exists()
    tidy = [record for record in state["records"] if record["event"] == "housekeeping"]
    assert len(tidy) == 1
    assert tidy[0]["builds"] == {"count": 1, "bytes": 1000}
    assert tidy[0]["recordings"] == {"count": 1, "bytes": 300}


def test_keep_recordings_days_flag_takes_zero_and_refuses_negative():
    for value, expected in (("0", 0.0), ("7", 7.0)):
        assert cli._keep_days(value) == expected
    with pytest.raises(Exception):
        cli._keep_days("-1")


@pytest.mark.asyncio
async def test_failed_cleaning_mark_still_closes_the_sessions_and_frees_the_phone(fakes, tmp_path, monkeypatch):
    from iphone_agent import locks
    events, state = fakes
    (tmp_path / "runner.json").write_text(json.dumps({"udid": UDID, "bundleId": BUNDLE, "appPath": "/signed/app"}))

    def full_disk(self):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(locks.PhoneLock, "mark_cleaning", full_disk)
    await cli.run_controller(args(tmp_path))
    assert "runner.close" in events and "audit.close" in events
    free = locks.PhoneLock(UDID, mode="serve", home=tmp_path / "other")
    await free.acquire(0.1)
    free.release()


@pytest.mark.asyncio
async def test_housekeeping_runs_after_the_first_connect_outside_its_deadline(fakes, tmp_path, monkeypatch):
    events, state = fakes
    (tmp_path / "runner.json").write_text(json.dumps({"udid": UDID, "bundleId": BUNDLE, "appPath": "/signed/app"}))
    from iphone_agent import housekeeping

    def tidy(home, days, audit):
        events.append("tidy")
        import time
        time.sleep(0.3)  # A large build takes a while to remove.

    monkeypatch.setattr(housekeeping, "tidy_state_directory", tidy)
    opening = keepawake.ActivityKeepAwake.open

    async def open_after_a_round_trip(self):
        await asyncio.sleep(0.01)  # The real keep-awake waits on the phone here.
        return await opening(self)

    monkeypatch.setattr(keepawake.ActivityKeepAwake, "open", open_after_a_round_trip)
    await cli.run_controller(args(tmp_path, no_keep_awake=False), first_connect_timeout=0.2)
    assert events.count("tidy") == 1 and events.index("wda.status") < events.index("tidy") < events.index("mcp.run")


def test_setup_requires_an_explicit_webdriveragent_source(monkeypatch, tmp_path, capsys):
    built = []
    monkeypatch.setattr("iphone_agent.setup.build_runner", lambda *a, **k: built.append(a))
    monkeypatch.setattr("sys.argv", ["iphone-agent", "--home", str(tmp_path), "setup", "--team", "TEAMID1234"])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 2 and not built
    assert "--source" in capsys.readouterr().err
