"""Xcode runner adopts only matching WDA and owns only its launched process."""

import asyncio
import contextlib
import errno
import os
import plistlib
import signal
import subprocess

import pytest

from iphone_agent import apple_runner as module


UDID = "00008120-0123456789ABCDEF"
BUNDLE_ID = "org.example.WDA.xctrunner"
PRODUCT_ID = "org.example.WDA"


def healthy(product=PRODUCT_ID):
    return {"ready": True, "state": "success", "build": {"productBundleIdentifier": product}}


class FakeWDA:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call(self, method, path):
        self.calls.append((method, path))
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def prepared(monkeypatch):
    """Record the developer-image step; its devicectl behavior is tested in test_network."""
    calls = []

    async def prepare(self, timeout):
        calls.append(timeout)

    monkeypatch.setattr(module.AppleWDARunner, "_prepare_device", prepare)
    return calls


@pytest.fixture
def product(tmp_path):
    products = tmp_path / "private-home/build/unique/Build/Products"
    app = products / "Debug-iphoneos/WebDriverAgentRunner-Runner.app"
    test_bundle = app / "PlugIns/WebDriverAgentRunner.xctest"
    test_bundle.mkdir(parents=True)
    with (app / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleIdentifier": BUNDLE_ID}, stream)
    with (test_bundle / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleIdentifier": PRODUCT_ID}, stream)
    target = {
        "TestHostBundleIdentifier": BUNDLE_ID,
        "TestHostPath": "__TESTROOT__/Debug-iphoneos/WebDriverAgentRunner-Runner.app",
        "TestBundlePath": "__TESTHOST__/PlugIns/WebDriverAgentRunner.xctest",
        "EnvironmentVariables": {"WDA_PRODUCT_BUNDLE_IDENTIFIER": PRODUCT_ID},
        "TestingEnvironmentVariables": {"paths": ["__TESTROOT__/Debug-iphoneos/Frameworks"]},
        "DependentProductPaths": [
            "__TESTROOT__/Debug-iphoneos/WebDriverAgentRunner-Runner.app",
            "__TESTROOT__/Debug-iphoneos/WebDriverAgentRunner-Runner.app/PlugIns/WebDriverAgentRunner.xctest",
        ],
    }
    plan = products / "WebDriverAgentRunner_macosx26.5-arm64.xctestrun"
    with plan.open("wb") as stream:
        plistlib.dump({"WebDriverAgentRunner": target, "__xctestrun_metadata__": {}}, stream)
    return app, plan, target, tmp_path / "private-home"


@pytest.mark.asyncio
async def test_adopts_matching_healthy_wda_without_spawning_or_stopping(product, monkeypatch, prepared):
    app, plan, _, home = product
    calls = []

    async def verify(self):
        calls.append("verify")

    async def unexpected(*args, **kwargs):
        raise AssertionError("xcodebuild must not start")

    monkeypatch.setattr(module.AppleWDARunner, "_verify_signature", verify)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", unexpected)
    wda = FakeWDA([healthy()])
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, wda, home=home)
    assert await runner.open() is runner
    assert runner.adopted and runner.process is None
    assert prepared == []  # An adopted WDA needs no developer-image step.
    assert runner.xctestrun == plan
    assert runner.launch_plan is None
    await runner.close()
    assert calls == ["verify"]
    assert not list(home.glob("wda-xcodebuild-*.log"))
    assert wda.calls == [("GET", "/status")]


@pytest.mark.asyncio
async def test_xctestrun_mismatch_fails_before_code_sign_or_launch(product, monkeypatch):
    app, plan, target, home = product
    target["TestHostPath"] = "__TESTROOT__/Debug-iphoneos/Other.app"
    with plan.open("wb") as stream:
        plistlib.dump({"WebDriverAgentRunner": target}, stream)

    async def unexpected(*args, **kwargs):
        raise AssertionError("external command must not start")

    monkeypatch.setattr(module.AppleWDARunner, "_verify_signature", unexpected)
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, FakeWDA([healthy()]), home=home)
    with pytest.raises(module.AppleRunnerError, match="host or test bundle path"):
        await runner.open()


@pytest.mark.asyncio
async def test_wrong_status_identity_fails_without_launch(product, monkeypatch):
    app, _, _, home = product

    async def verify(self):
        pass

    async def unexpected(*args, **kwargs):
        raise AssertionError("xcodebuild must not start")

    monkeypatch.setattr(module.AppleWDARunner, "_verify_signature", verify)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", unexpected)
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, FakeWDA([healthy("org.other.WDA")]), home=home)
    with pytest.raises(module.AppleRunnerError, match="expected"):
        await runner.open()


class RunningProcess:
    pid = 23456

    def __init__(self):
        self.returncode = None
        self.stopped = asyncio.Event()

    async def wait(self):
        await self.stopped.wait()
        return self.returncode


@pytest.mark.asyncio
async def test_launches_exact_plan_and_kills_only_owned_group_on_close(product, monkeypatch):
    app, plan, _, home = product
    original_plan = plan.read_bytes()
    process = RunningProcess()
    commands = []
    signals = []

    async def verify(self):
        pass

    async def spawn(*command, **kwargs):
        commands.append((command, kwargs))
        return process

    def killpg(pid, sig):
        signals.append((pid, sig))
        process.returncode = -15
        process.stopped.set()

    monkeypatch.setattr(module.AppleWDARunner, "_verify_signature", verify)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module.os, "killpg", killpg)
    wda = FakeWDA([ConnectionError("not yet"), healthy()])
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, wda, home=home,
                                   bind_host="fd00:db8:1::1", port=8123)
    await runner.open(timeout=2)
    assert not runner.adopted and runner.process is process
    command, options = commands[0]
    assert runner.launch_plan is not None
    launch = runner.launch_plan
    assert command == ("xcodebuild", "test-without-building", "-xctestrun", str(launch),
                       "-destination", f"id={UDID}", "-collect-test-diagnostics", "never")
    assert launch.parent == home
    assert os.stat(launch).st_mode & 0o777 == 0o600
    with launch.open("rb") as stream:
        launch_target = plistlib.load(stream)["WebDriverAgentRunner"]
    products = app.parent.parent
    assert launch_target["TestHostPath"] == str(app)
    assert launch_target["DependentProductPaths"][0] == str(app)
    assert launch_target["TestingEnvironmentVariables"]["paths"] == [str(products / "Debug-iphoneos/Frameworks")]
    assert launch_target["EnvironmentVariables"]["USE_IP"] == "fd00:db8:1::1"
    assert launch_target["EnvironmentVariables"]["USE_PORT"] == "8123"
    assert plan.read_bytes() == original_plan
    assert options["start_new_session"] is True
    assert options["stderr"] == asyncio.subprocess.STDOUT
    assert runner.log_path.is_file()
    assert os.stat(runner.log_path).st_mode & 0o777 == 0o600
    await runner.close()
    assert signals == [(23456, signal.SIGTERM), (23456, signal.SIGKILL)]
    assert runner.process is None
    assert not launch.exists()


@pytest.mark.asyncio
async def test_startup_timeout_stops_owned_xcodebuild(product, monkeypatch):
    app, _, _, home = product
    process = RunningProcess()
    signals = []

    async def verify(self):
        pass

    async def spawn(*command, **kwargs):
        return process

    def killpg(pid, sig):
        signals.append((pid, sig))
        process.returncode = -15
        process.stopped.set()

    monkeypatch.setattr(module.AppleWDARunner, "_verify_signature", verify)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module.os, "killpg", killpg)
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, FakeWDA([ConnectionError("offline")]), home=home)
    with pytest.raises(module.AppleRunnerError, match="did not become ready"):
        await runner.open(timeout=0.03)
    assert signals == [(23456, signal.SIGTERM), (23456, signal.SIGKILL)]
    assert runner.process is None
    assert runner.launch_plan is None
    assert not list(home.glob("wda-launch-*.xctestrun"))


@pytest.mark.asyncio
async def test_losing_the_cable_during_the_wait_stops_owned_xcodebuild(product, monkeypatch):
    from iphone_agent import cli
    from iphone_agent.network import UsbLinkLost
    app, _, _, home = product
    process = RunningProcess()
    signals = []

    async def verify(self):
        pass

    async def spawn(*command, **kwargs):
        return process

    def killpg(pid, sig):
        signals.append((pid, sig))
        process.returncode = -15
        process.stopped.set()

    async def gone(self):
        return False

    monkeypatch.setattr(module.AppleWDARunner, "_verify_signature", verify)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module.os, "killpg", killpg)
    monkeypatch.setattr(cli.DeviceServices, "usb_present", gone)
    monkeypatch.setattr(cli, "USB_RECHECK_INTERVAL", 0.05)
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, FakeWDA([ConnectionError("offline")]), home=home)
    with pytest.raises(UsbLinkLost, match="the USB link disappeared"):
        await cli._open_while_usb_present(runner.open(timeout=30), UDID)
    assert signals == [(23456, signal.SIGTERM), (23456, signal.SIGKILL)]
    assert runner.process is None and runner.launch_plan is None
    assert not list(home.glob("wda-launch-*.xctestrun"))


@pytest.mark.asyncio
async def test_cancelled_ui_automation_prompt_is_named_in_the_exit_error(product, monkeypatch):
    app, _, _, home = product
    process = RunningProcess()
    process.returncode = 65

    async def verify(self):
        pass

    async def spawn(*command, **kwargs):
        # The line xcodebuild logged on iOS 26.6.2 when the phone locked during launch.
        kwargs["stdout"].write(b"WebDriverAgentRunner-Runner[9433:2606887] [Default] Failed to initialize for UI "
                               b"testing: Error Domain=com.apple.LocalAuthentication Code=-2 \"Canceled by user.\"\n")
        kwargs["stdout"].flush()
        return process

    monkeypatch.setattr(module.AppleWDARunner, "_verify_signature", verify)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, FakeWDA([ConnectionError("offline")]), home=home)
    with pytest.raises(module.UIAutomationCancelled, match=r"exited before readiness \(65\).*did not approve UI automation"):
        await runner.open(timeout=2)


async def test_stopping_the_runner_leaves_no_child_that_outlived_its_leader(tmp_path):
    ready, named = tmp_path / "ready", tmp_path / "child"
    # The child ignores SIGTERM, as a child forked just as the signal arrives misses it.
    process = await asyncio.create_subprocess_exec(
        "/bin/bash", "-c", f"(trap '' TERM; echo yes > {ready}; exec /bin/sleep 3600) & echo $! > {named}; wait",
        stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
    while not ready.exists() or not named.exists() or not named.read_text().strip():
        await asyncio.sleep(0.01)
    child = int(named.read_text())
    try:
        await asyncio.wait_for(module._stop_owned_process(process), 10)
        assert process.returncode is not None
        for _ in range(500):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("the child outlived its leader")
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)


async def test_stopping_a_leader_that_just_exited_is_not_a_failure():
    leader = subprocess.Popen(["/usr/bin/true"], start_new_session=True)
    os.waitid(os.P_PID, leader.pid, os.WEXITED | os.WNOWAIT)  # It has exited, and it stays unreaped.

    class Unreaped:
        pid = leader.pid
        returncode = None

        async def wait(self):
            self.returncode = leader.wait()
            return self.returncode

    process = Unreaped()
    await asyncio.wait_for(module._stop_owned_process(process), 5)
    assert process.returncode == 0


@pytest.mark.parametrize(("leaves_on_term", "refused"), [(True, 0), (False, 1), (True, 1)],
                         ids=["first-signal", "escalation", "final-sweep"])
async def test_refused_signal_does_not_fail_the_stop(monkeypatch, leaves_on_term, refused):
    process = RunningProcess()
    signals = []

    def killpg(pid, sig):
        signals.append(sig)
        if leaves_on_term or sig == signal.SIGKILL:
            process.returncode = -sig
            process.stopped.set()
        if len(signals) - 1 == refused:
            raise PermissionError(errno.EPERM, "Operation not permitted")

    async def out_of_time(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(module.os, "killpg", killpg)
    if not leaves_on_term:
        monkeypatch.setattr(module.asyncio, "wait_for", out_of_time)
    await module._stop_owned_process(process)
    assert process.returncode is not None
    assert signals == ([signal.SIGTERM, signal.SIGKILL] if leaves_on_term else [signal.SIGTERM, signal.SIGKILL, signal.SIGKILL])


async def test_stop_waits_for_the_killed_leader_before_it_returns(monkeypatch):
    process = RunningProcess()
    signals = []

    async def out_of_time(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(module.asyncio, "wait_for", out_of_time)
    stopping = asyncio.create_task(module._stop_owned_process(process))
    for _ in range(10):
        await asyncio.sleep(0)
    returned_early, signalled_early = stopping.done(), list(signals)
    process.returncode = -signal.SIGKILL
    process.stopped.set()  # The leader is gone only now.
    await stopping
    assert not returned_early
    assert signalled_early == [signal.SIGTERM, signal.SIGKILL]
    assert signals == [signal.SIGTERM, signal.SIGKILL, signal.SIGKILL]  # The sweep comes after the leader is gone.


async def test_other_xcodebuild_exit_is_not_a_cancelled_prompt(product, monkeypatch):
    app, _, _, home = product
    process = RunningProcess()
    process.returncode = 70

    async def verify(self):
        pass

    async def spawn(*command, **kwargs):
        return process

    monkeypatch.setattr(module.AppleWDARunner, "_verify_signature", verify)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, FakeWDA([ConnectionError("offline")]), home=home)
    with pytest.raises(module.AppleRunnerError, match=r"exited before readiness \(70\)") as raised:
        await runner.open(timeout=2)
    assert not isinstance(raised.value, module.UIAutomationCancelled)


def test_rejects_non_literal_bind_host(product):
    app, _, _, home = product
    with pytest.raises(ValueError, match="literal"):
        module.AppleWDARunner(UDID, BUNDLE_ID, app, FakeWDA([healthy()]),
                              home=home, bind_host="phone.local")


@pytest.mark.asyncio
async def test_cancellation_during_spawn_still_owns_and_stops_child(product, monkeypatch):
    app, _, _, home = product
    process = RunningProcess()
    spawning = asyncio.Event()
    release_spawn = asyncio.Event()
    signals = []

    async def verify(self):
        pass

    async def spawn(*command, **kwargs):
        spawning.set()
        await release_spawn.wait()
        return process

    def killpg(pid, sig):
        signals.append((pid, sig))
        process.returncode = -15
        process.stopped.set()

    monkeypatch.setattr(module.AppleWDARunner, "_verify_signature", verify)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module.os, "killpg", killpg)
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, FakeWDA([ConnectionError("offline")]), home=home)
    task = asyncio.create_task(runner.open())
    await asyncio.wait_for(spawning.wait(), timeout=1)
    task.cancel()
    release_spawn.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert signals == [(23456, signal.SIGTERM), (23456, signal.SIGKILL)]
    assert runner.process is None


async def test_locked_phone_fails_before_xcodebuild_with_a_recoverable_error(product, monkeypatch):
    from iphone_agent import network
    app, _, _, home = product

    async def verify(self):
        pass

    async def locked(udid, timeout):
        assert (udid, timeout) == (UDID, 30.0)  # Bounded even under a long startup timeout.
        raise network.DeveloperImageLockedError("unlock the phone")

    async def reads_unlocked(udid, timeout):
        return False  # The phone locked between the two queries.

    async def unexpected(*args, **kwargs):
        raise AssertionError("xcodebuild must not start")

    monkeypatch.undo()  # Drop the autouse stub: use the real _prepare_device and stub only devicectl.
    monkeypatch.setattr(module.AppleWDARunner, "_verify_signature", verify)
    monkeypatch.setattr(network, "ensure_developer_image", locked)
    monkeypatch.setattr(network, "device_passcode_required", reads_unlocked)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", unexpected)
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, FakeWDA([ConnectionError("not running")]), home=home)
    with pytest.raises(network.DeveloperImageLockedError) as raised:
        await runner.open(timeout=300)
    assert network.is_recoverable_error(raised.value)
    assert runner.process is None and not list(home.glob("wda-xcodebuild-*.log"))


async def test_locked_phone_with_the_developer_image_mounted_fails_before_xcodebuild(product, monkeypatch):
    """xcodebuild waits silently on a locked phone, so the lock is read before it starts."""
    from iphone_agent import network
    app, _, _, home = product
    order = []

    async def verify(self):
        pass

    async def mounted(udid, timeout):
        order.append("image")

    async def passcode_required(udid, timeout):
        assert (udid, timeout) == (UDID, 30.0)  # Bounded even under a long startup timeout.
        order.append("lock")
        return True

    async def unexpected(*args, **kwargs):
        raise AssertionError("xcodebuild must not start")

    monkeypatch.undo()  # Drop the autouse stub: use the real _prepare_device and stub only devicectl.
    monkeypatch.setattr(module.AppleWDARunner, "_verify_signature", verify)
    monkeypatch.setattr(network, "ensure_developer_image", mounted)
    monkeypatch.setattr(network, "device_passcode_required", passcode_required)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", unexpected)
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, FakeWDA([ConnectionError("not running")]), home=home)
    with pytest.raises(network.DeviceLockedError, match="locked.*unlock") as raised:
        await runner.open(timeout=300)
    assert network.is_recoverable_error(raised.value)
    assert order == ["lock"]  # A locked phone is not asked to mount anything.
    assert runner.process is None and not list(home.glob("wda-xcodebuild-*.log"))


async def test_unlocked_phone_is_prepared_for_launch(product, monkeypatch):
    from iphone_agent import network
    app, _, _, home = product
    order = []

    async def mounted(udid, timeout):
        order.append(("image", udid, timeout))

    async def passcode_required(udid, timeout):
        order.append(("lock", udid, timeout))
        return False

    monkeypatch.undo()
    monkeypatch.setattr(network, "ensure_developer_image", mounted)
    monkeypatch.setattr(network, "device_passcode_required", passcode_required)
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, FakeWDA([ConnectionError("not running")]), home=home)
    await runner._prepare_device(12.0)
    assert order == [("lock", UDID, 12.0), ("image", UDID, 12.0)]


def test_a_locked_developer_image_is_a_locked_phone():
    from iphone_agent import network
    assert issubclass(network.DeveloperImageLockedError, network.DeviceLockedError)
    assert network.is_recoverable_error(network.DeviceLockedError("locked"))



async def test_phone_that_locks_while_xcode_starts_the_runner_ends_the_launch_as_a_locked_phone(product, monkeypatch):
    """The lock is read before the launch; a phone that locks just after leaves xcodebuild waiting for the unlock."""
    from iphone_agent import network
    app, _, _, home = product
    process = RunningProcess()
    signals = []

    async def verify(self):
        pass

    async def spawn(*command, **kwargs):
        # What xcodebuild logged on iOS 27.2 while it waited on the locked phone.
        kwargs["stdout"].write(
            b"NSLocalizedRecoverySuggestion=Xcode cannot launch WebDriverAgentRunner on My iPhone because the device is "
            b"locked., NSLocalizedDescription=Unlock My iPhone to Continue\n"
            b"[MT] Run Destination Preflight: Waiting for the destination to become ready.\n")
        kwargs["stdout"].flush()
        return process

    def killpg(pid, sig):
        signals.append((pid, sig))
        process.returncode = -15
        process.stopped.set()

    async def still_locked(udid, timeout):
        return True

    monkeypatch.setattr(module.AppleWDARunner, "_verify_signature", verify)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module.os, "killpg", killpg)
    monkeypatch.setattr(network, "device_passcode_required", still_locked)
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, FakeWDA([ConnectionError("offline")]), home=home)
    started = asyncio.get_running_loop().time()
    with pytest.raises(network.DeviceLockedError, match="locked while the WDA runner was starting"):
        await runner.open(timeout=300)
    assert asyncio.get_running_loop().time() - started < 10  # Not the 300-second startup timeout.
    assert signals and signals[0] == (23456, signal.SIGTERM)  # xcodebuild does not stay behind, waiting.
    assert runner.process is None


LOCKED_PREFLIGHT = (b"NSLocalizedRecoverySuggestion=Xcode cannot launch WebDriverAgentRunner on My iPhone because the "
                    b"device is locked., NSLocalizedDescription=Unlock My iPhone to Continue\n")


def waiting_launch(monkeypatch, process, signals):
    async def verify(self):
        pass

    async def spawn(*command, **kwargs):
        kwargs["stdout"].write(LOCKED_PREFLIGHT)
        kwargs["stdout"].flush()
        return process

    def killpg(pid, sig):
        signals.append((pid, sig))
        process.returncode = -15
        process.stopped.set()

    monkeypatch.setattr(module.AppleWDARunner, "_verify_signature", verify)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module.os, "killpg", killpg)


async def test_owner_unlocking_while_xcode_waits_lets_the_same_launch_finish(product, monkeypatch):
    """Xcode's "device is locked" line stays in the log after the unlock, so the phone itself is asked."""
    from iphone_agent import network
    app, _, _, home = product
    process, signals, queries = RunningProcess(), [], []
    waiting_launch(monkeypatch, process, signals)

    async def unlocked_by_now(udid, timeout):
        queries.append(asyncio.get_running_loop().time())
        return False

    monkeypatch.setattr(network, "device_passcode_required", unlocked_by_now)
    monkeypatch.setattr(module, "LOCK_CONFIRM_INTERVAL", 0.6)
    wda = FakeWDA([ConnectionError("offline")] * 4 + [healthy()])
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, wda, home=home)
    await runner.open(timeout=30)
    assert runner.process is process and signals == []
    assert 1 <= len(queries) <= 3  # Asked again only every LOCK_CONFIRM_INTERVAL, not at every probe.
    await runner.close()


async def test_xcode_waiting_on_a_lock_the_phone_cannot_confirm_is_still_a_locked_phone(product, monkeypatch):
    """A phone asleep on Wi-Fi can fail the lock query; Xcode's own statement then stands."""
    from iphone_agent import network
    app, _, _, home = product
    process, signals = RunningProcess(), []
    waiting_launch(monkeypatch, process, signals)

    async def unreachable(udid, timeout):
        raise network.CoreDeviceUnavailableError("CoreDevice lockState query failed with exit status 1")

    monkeypatch.setattr(network, "device_passcode_required", unreachable)
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, FakeWDA([ConnectionError("offline")]), home=home)
    with pytest.raises(network.DeviceLockedError, match="locked while the WDA runner was starting"):
        await runner.open(timeout=300)
    assert signals and runner.process is None


@pytest.mark.parametrize("failure", ["CoreDeviceUnavailableError", "NetworkBindingError", "CoreDeviceTimeoutError"])
async def test_lock_query_that_fails_before_launch_does_not_stop_the_launch(product, monkeypatch, failure):
    """The query is a shortcut to a clear error; without an answer the launch goes ahead as before."""
    from iphone_agent import network
    app, _, _, home = product
    order = []

    async def mounted(udid, timeout):
        order.append("image")

    async def no_answer(udid, timeout):
        order.append("lock")
        raise getattr(network, failure)("CoreDevice returned invalid device lock state")

    monkeypatch.undo()
    monkeypatch.setattr(network, "ensure_developer_image", mounted)
    monkeypatch.setattr(network, "device_passcode_required", no_answer)
    runner = module.AppleWDARunner(UDID, BUNDLE_ID, app, FakeWDA([ConnectionError("not running")]), home=home)
    await runner._prepare_device(12.0)
    assert order == ["lock", "image"]

