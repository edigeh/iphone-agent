"""Own one Xcode-managed WDA XCTest run on an explicitly selected iPhone.

The xctestrun plan and signed app come from the same saved build product. An
already healthy WDA with the expected bundle identity is adopted without
starting or stopping another test process.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
from pathlib import Path
import plistlib
import signal
import time
import uuid
from typing import Any

import aiohttp

from .wda import WDAError


class AppleRunnerError(RuntimeError):
    pass


class UIAutomationCancelled(AppleRunnerError):
    """The phone was locked or its owner dismissed the Face ID or passcode prompt for UI automation."""


# iOS asks for Face ID or the passcode before it lets XCTest drive the UI. A
# locked phone or a dismissed prompt cancels it and xcodebuild exits with 65.
UI_AUTOMATION_AUTH_CANCELLED = "Failed to initialize for UI testing: Error Domain=com.apple.LocalAuthentication"


# xcodebuild logs this and then waits, without exiting, until the phone is unlocked.
XCODE_WAITING_ON_LOCK = "because the device is locked"
LOCK_CONFIRM_INTERVAL = 3.0


def _log_tail(log_path: Path | None) -> str:
    try:
        with open(log_path, "rb") as log:  # type: ignore[arg-type]
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 65536))
            return log.read().decode("utf-8", "replace")
    except (OSError, TypeError):
        return ""


def _exit_reason(log_path: Path | None) -> str:
    tail = _log_tail(log_path)
    if UI_AUTOMATION_AUTH_CANCELLED in tail:
        return ("; the iPhone did not approve UI automation (its Face ID or passcode prompt was cancelled "
                "or the phone was locked). Unlock it and approve the prompt")
    return ""


KEPT_LAUNCH_LOGS = 20
STALE_LAUNCH_PLAN_SECONDS = 24 * 3600.0


def _prune_launch_files(home: Path) -> None:
    """Every launch writes a log, and a killed session leaves its launch plan; keep the recent ones."""
    def modified(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    logs = sorted(home.glob("wda-xcodebuild-*.log"), key=modified, reverse=True)
    cutoff = time.time() - STALE_LAUNCH_PLAN_SECONDS
    stale_plans = [path for path in home.glob("wda-launch-*.xctestrun") if modified(path) < cutoff]
    for path in [*logs[KEPT_LAUNCH_LOGS - 1:], *stale_plans]:
        with contextlib.suppress(OSError):
            path.unlink()


async def _stop_owned_process(process: asyncio.subprocess.Process) -> None:
    """Terminate only the process group created for this runner."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):  # EPERM: the leader has exited and nothing has reaped it yet.
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        await process.wait()
    # A child forked as the signal arrived, or one that ignores it, would outlive its leader.
    with contextlib.suppress(ProcessLookupError, PermissionError):  # macOS says EPERM for a group of only zombies.
        os.killpg(process.pid, signal.SIGKILL)


def _bundle_id(info_path: Path) -> str:
    if not info_path.is_file():
        raise AppleRunnerError(f"Missing runner Info.plist: {info_path}")
    try:
        with info_path.open("rb") as stream:
            value = plistlib.load(stream).get("CFBundleIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise AppleRunnerError(f"Invalid runner Info.plist: {info_path}") from exc
    if not isinstance(value, str) or not value:
        raise AppleRunnerError(f"Runner has no bundle ID: {info_path}")
    return value


class AppleWDARunner:
    """Start/adopt the exact installed WDA runner and keep owned xcodebuild alive."""

    def __init__(self, udid: str, bundle_id: str, app_path: str | Path, wda_client: Any,
                 *, home: str | Path | None = None, bind_host: str | None = None, port: int = 8100):
        if not isinstance(udid, str) or not udid:
            raise ValueError("An explicit hardware UDID is required")
        if not isinstance(bundle_id, str) or not bundle_id.endswith(".xctrunner"):
            raise ValueError("Runner bundle ID must end in .xctrunner")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("WDA port must be between 1 and 65535")
        if bind_host is not None:
            try:
                if "%" in bind_host:
                    raise ValueError("scoped address")
                bind_host = str(ipaddress.ip_address(bind_host))
            except (ValueError, TypeError) as exc:
                raise ValueError("WDA bind host must be a literal unscoped IP address") from exc
        self.udid = udid
        self.bundle_id = bundle_id
        self.product_bundle_id = bundle_id.removesuffix(".xctrunner")
        self.app_path = Path(app_path).expanduser().resolve()
        self.wda = wda_client
        self.home = Path(home).expanduser().resolve() if home is not None else Path.home() / ".iphone-agent"
        self.bind_host = bind_host
        self.port = port
        self.xctestrun: Path | None = None
        self.launch_plan: Path | None = None
        self.log_path: Path | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.status: dict[str, Any] | None = None
        self.adopted = False
        self._open = False
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "AppleWDARunner":
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _validate_product(self) -> Path:
        app = self.app_path
        if app.name != "WebDriverAgentRunner-Runner.app" or app.parent.name != "Debug-iphoneos" or not app.is_dir():
            raise AppleRunnerError(f"Selected WDA runner app is missing or unexpected: {app}")
        if _bundle_id(app / "Info.plist") != self.bundle_id:
            raise AppleRunnerError("Selected app bundle ID does not match the requested runner")
        test_bundle = app / "PlugIns/WebDriverAgentRunner.xctest"
        if not test_bundle.is_dir() or _bundle_id(test_bundle / "Info.plist") != self.product_bundle_id:
            raise AppleRunnerError("Selected runner has no matching WebDriverAgent test bundle")
        products = app.parent.parent
        plans = [path for path in products.glob("*.xctestrun") if path.resolve().parent == products.resolve()]
        if len(plans) != 1:
            raise AppleRunnerError(f"Expected one xctestrun plan beside the selected app, found {len(plans)}")
        plan = plans[0]
        try:
            with plan.open("rb") as stream:
                value = plistlib.load(stream)
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            raise AppleRunnerError(f"Invalid xctestrun plan: {plan}") from exc
        if not isinstance(value, dict) or set(value) - {"WebDriverAgentRunner", "__xctestrun_metadata__"}:
            raise AppleRunnerError("xctestrun plan includes unexpected test targets")
        target = value.get("WebDriverAgentRunner")
        if not isinstance(target, dict) or target.get("TestHostBundleIdentifier") != self.bundle_id:
            raise AppleRunnerError("xctestrun runner bundle ID does not match the selected app")
        expected_host = "__TESTROOT__/Debug-iphoneos/WebDriverAgentRunner-Runner.app"
        expected_test = "__TESTHOST__/PlugIns/WebDriverAgentRunner.xctest"
        if target.get("TestHostPath") != expected_host or target.get("TestBundlePath") != expected_test:
            raise AppleRunnerError("xctestrun host or test bundle path does not point to the selected app")
        if target.get("CommandLineArguments", []) or target.get("UITargetAppCommandLineArguments", []):
            raise AppleRunnerError("xctestrun contains unexpected launch arguments")
        environment = target.get("EnvironmentVariables")
        if not isinstance(environment, dict) or environment.get("WDA_PRODUCT_BUNDLE_IDENTIFIER") != self.product_bundle_id:
            raise AppleRunnerError("xctestrun WDA product bundle ID does not match the selected app")
        dependencies = target.get("DependentProductPaths")
        if not isinstance(dependencies, list) or expected_host not in dependencies or expected_host + "/PlugIns/WebDriverAgentRunner.xctest" not in dependencies:
            raise AppleRunnerError("xctestrun dependencies omit the selected runner or test bundle")
        if any(not isinstance(path, str) or not path.startswith("__TESTROOT__/Debug-iphoneos/") or ".." in path.split("/") for path in dependencies):
            raise AppleRunnerError("xctestrun has a dependency outside the selected build products")
        self.xctestrun = plan
        return plan

    @staticmethod
    def _replace_testroot(value: Any, products: Path) -> Any:
        if isinstance(value, str):
            return value.replace("__TESTROOT__", str(products))
        if isinstance(value, dict):
            return {key: AppleWDARunner._replace_testroot(item, products) for key, item in value.items()}
        if isinstance(value, list):
            return [AppleWDARunner._replace_testroot(item, products) for item in value]
        return value

    def _write_launch_plan(self, original: Path) -> Path:
        """Pin the relocated plan to the selected products and WDA interface."""
        with original.open("rb") as stream:
            plan = plistlib.load(stream)
        products = self.app_path.parent.parent
        plan = self._replace_testroot(plan, products)
        environment = plan["WebDriverAgentRunner"]["EnvironmentVariables"]
        if self.bind_host is not None:
            environment["USE_IP"] = self.bind_host
        environment["USE_PORT"] = str(self.port)
        path = self.home / f"wda-launch-{uuid.uuid4().hex}.xctestrun"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                plistlib.dump(plan, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    async def _verify_signature(self) -> None:
        process = await asyncio.create_subprocess_exec(
            "codesign", "--verify", "--deep", "--strict", str(self.app_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            status = await asyncio.wait_for(process.wait(), timeout=30)
        except BaseException:
            await _stop_owned_process(process)
            raise
        if status != 0:
            raise AppleRunnerError(f"Selected WDA app failed code signature verification ({status})")

    def _validate_status(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("ready") is not True or value.get("state") != "success":
            raise AppleRunnerError("WDA endpoint answered but is not healthy")
        build = value.get("build")
        actual = build.get("productBundleIdentifier") if isinstance(build, dict) else None
        if actual != self.product_bundle_id:
            raise AppleRunnerError(f"WDA endpoint serves {actual!r}, expected {self.product_bundle_id!r}")
        return value

    async def _probe(self, timeout: float = 5.0) -> dict[str, Any] | None:
        try:
            value = await asyncio.wait_for(self.wda.call("GET", "/status"), timeout=timeout)
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
            return None
        except WDAError as exc:
            raise AppleRunnerError(f"WDA endpoint returned an unexpected response: {exc.code}") from exc
        return self._validate_status(value)

    async def _prepare_device(self, timeout: float) -> None:
        from . import network
        budget = min(30.0, timeout)
        # On a locked phone xcodebuild shows "Unlock to Continue" and waits without failing,
        # so the lock is read here, where the caller can be told. Without an answer the launch
        # goes ahead, and open() watches for that wait.
        try:
            locked = await network.device_passcode_required(self.udid, budget)
        except network.NetworkBindingError:
            locked = False
        if locked:
            raise network.DeviceLockedError("The iPhone is locked, so the WDA runner cannot start on it; unlock the phone")
        await network.ensure_developer_image(self.udid, budget)

    async def _still_locked(self) -> bool:
        """Whether the phone Xcode is waiting on is locked now; Xcode's line stays in the log after an unlock."""
        from . import network
        try:
            return await network.device_passcode_required(self.udid, 10.0)
        except network.NetworkBindingError:
            return True  # A phone asleep on Wi-Fi can fail the query; Xcode's own statement stands.

    async def open(self, *, timeout: float = 120.0) -> "AppleWDARunner":
        if timeout <= 0:
            raise ValueError("Startup timeout must be positive")
        async with self._lock:
            if self._open:
                if self.process is not None and self.process.returncode is not None:
                    raise AppleRunnerError("Owned WDA xcodebuild process exited unexpectedly")
                return self
            plan = self._validate_product()
            await self._verify_signature()
            existing = await self._probe()
            if existing is not None:
                self.status = existing
                self.adopted = True
                self._open = True
                return self
            await self._prepare_device(timeout)
            self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
            _prune_launch_files(self.home)
            self.log_path = self.home / f"wda-xcodebuild-{uuid.uuid4().hex}.log"
            try:
                self.launch_plan = self._write_launch_plan(plan)
                fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as log:
                    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                        "xcodebuild", "test-without-building", "-xctestrun", str(self.launch_plan),
                        "-destination", f"id={self.udid}",
                        "-collect-test-diagnostics", "never",
                        stdout=log, stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True,
                    ))
                    try:
                        self.process = await asyncio.shield(spawn)
                    except asyncio.CancelledError:
                        # Child creation may outlive cancellation. Take ownership
                        # before the outer cleanup terminates its process group.
                        self.process = await spawn
                        raise
                deadline = asyncio.get_running_loop().time() + timeout
                confirm_lock_at = 0.0
                while True:
                    process = self.process
                    if process is None or process.returncode is not None:
                        code = None if process is None else process.returncode
                        reason = _exit_reason(self.log_path)
                        raise (UIAutomationCancelled if reason else AppleRunnerError)(
                            f"WDA xcodebuild exited before readiness ({code}); see {self.log_path}{reason}")
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise AppleRunnerError(f"WDA did not become ready within {timeout:g}s; see {self.log_path}")
                    status = await self._probe(timeout=min(5.0, remaining))
                    if status is not None:
                        await asyncio.sleep(0)
                        if self.process.returncode is not None:
                            raise AppleRunnerError(f"WDA xcodebuild exited during readiness; see {self.log_path}")
                        self.status = status
                        self._open = True
                        return self
                    if (asyncio.get_running_loop().time() >= confirm_lock_at
                            and XCODE_WAITING_ON_LOCK in _log_tail(self.log_path)):
                        if await self._still_locked():
                            from .network import DeviceLockedError
                            raise DeviceLockedError(
                                "The iPhone locked while the WDA runner was starting; unlock the phone")
                        # Unlocked since: Xcode carries on by itself.
                        confirm_lock_at = asyncio.get_running_loop().time() + LOCK_CONFIRM_INTERVAL
                    await asyncio.sleep(min(0.5, max(0.0, deadline - asyncio.get_running_loop().time())))
            except BaseException:
                if self.process is not None:
                    await _stop_owned_process(self.process)
                    self.process = None
                if self.launch_plan is not None:
                    self.launch_plan.unlink(missing_ok=True)
                    self.launch_plan = None
                raise

    async def close(self) -> None:
        async with self._lock:
            process, self.process = self.process, None
            self._open = False
            self.status = None
            self.adopted = False
            try:
                if process is not None:
                    await _stop_owned_process(process)
            finally:
                if self.launch_plan is not None:
                    self.launch_plan.unlink(missing_ok=True)
                    self.launch_plan = None
