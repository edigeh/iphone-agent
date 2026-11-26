"""Command-line, daemon and MCP entry points."""
import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import secrets
import sys
import traceback
from pathlib import Path
from aiohttp import ClientConnectionError, ClientSession, web
from .audit import AuditLog, AuditLogBusy
from .core import ConnectionUnavailable, Controller, ControllerHost, PhoneLocked
from .device import DeviceServices
from .transport import USBForward
from .wda import WDAClient

_log = logging.getLogger(__name__)

DEFAULT_HOME = Path.home() / ".iphone-agent"


class SetupError(RuntimeError):
    """The saved runner or the arguments are wrong; no link or unlock can fix it."""


def _idle_seconds(text):
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a finite number of seconds, 0 or more; 0 never times out")
    return value


def _keep_days(text):
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a finite number of days, 0 or more; 0 keeps every recording")
    return value


def read_token(home):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = home / "token"
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(32))
    return os.environ.get("IPHONE_AGENT_TOKEN") or path.read_text().strip()


async def doctor():
    devices = await DeviceServices.discover()
    result = {"usbDevices": devices, "usbReady": False}
    if len(devices) == 1:
        device = DeviceServices(devices[0]["udid"])
        try:
            values = await device.info()
            result["device"] = {key: values.get(key) for key in ("ProductType", "ProductVersion", "DeveloperModeStatus")}
            result["usbReady"] = True
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            await device.close()
    elif not devices:
        result["action"] = "Connect iPhone by USB, unlock it and trust this Mac; enable Developer Mode."
    else:
        result["action"] = "Select a device with --udid."
    return result


CONNECTION_CLEANUP_TIMEOUT = 30.0
STARTUP_POLL_INTERVAL = 1.0
# How long a connect keeps trying a locked phone before it says so. The owner has to unlock it,
# and a caller that is told at once can ask; one that waits out the startup timeout cannot.
LOCKED_CONNECT_GRACE = 20.0
IDLE_RECHECK_INTERVAL = 5.0  # How soon an overdue idle disconnect that had to wait looks again.
USB_RECHECK_INTERVAL = 2.0
USB_RECHECK_TIMEOUT = 5.0
USB_RECHECK_MISSES = 2  # usbmuxd can list nothing for a moment with the phone attached, or fail once.


class _OwnedSession:
    def __init__(self):
        self.device = self.forward = self.wda = self.runner = self.controller = self.binding = None

    async def close(self):
        task = asyncio.create_task(self._close(), name="iphone-agent-session-cleanup")
        cancelled = []
        while True:
            try:
                errors = await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                if task.cancelled():
                    raise
                cancelled.append(exc)
        if cancelled:
            if len(cancelled) == 1 and not errors:
                raise cancelled[0]
            raise BaseExceptionGroup("Device session cleanup cancelled", [*cancelled, *errors])
        return errors

    async def _close(self):
        errors = []
        if self.controller is not None:
            try:
                await self.controller.close(close_audit=False, resource_timeout=CONNECTION_CLEANUP_TIMEOUT)
            except BaseException as exc:
                errors.append(exc)
        else:
            for resource in (self.wda, self.device):
                if resource is not None:
                    try:
                        async with asyncio.timeout(CONNECTION_CLEANUP_TIMEOUT):
                            await resource.close()
                    except BaseException as exc:
                        errors.append(exc)
        for resource in (self.forward, self.runner):
            if resource is not None:
                try:
                    async with asyncio.timeout(CONNECTION_CLEANUP_TIMEOUT):
                        await resource.close()
                except BaseException as exc:
                    errors.append(exc)
        return errors


async def _usb_present(udid):
    try:
        return await DeviceServices(udid).usb_present()
    except Exception as exc:
        # Same rule as capabilities: an unreadable usbmuxd list means no usable cable.
        _log.warning("USB presence check failed, using the paired network: %s", _describe(exc))
        return False


async def _usb_lost(udid):
    """Return once consecutive checks found no cable. A failed check counts the same as a phone not listed."""
    misses = 0
    while True:
        await asyncio.sleep(USB_RECHECK_INTERVAL)
        try:
            async with asyncio.timeout(USB_RECHECK_TIMEOUT):
                present = await DeviceServices(udid).usb_present()
        except Exception as exc:
            # Same rule as _usb_present: an unreadable usbmuxd list means no usable cable.
            misses += 1
            reason = "USB presence could not be read"
            _log.warning("USB presence check failed (%d in a row): %s", misses, _describe(exc))
        else:
            if present:
                misses = 0
                continue
            misses += 1
            reason = "the USB link disappeared"
            _log.warning("The phone is not listed on USB (%d in a row)", misses)
        if misses >= USB_RECHECK_MISSES:
            return reason


async def _open_while_usb_present(opening, udid):
    """Wait for a USB runner; losing the cable ends the wait, which stops that runner's xcodebuild."""
    from .network import UsbLinkLost
    opener, watcher = asyncio.ensure_future(opening), asyncio.create_task(_usb_lost(udid))
    try:
        await asyncio.wait([opener, watcher], return_when=asyncio.FIRST_COMPLETED)
        if opener.done():
            return opener.result()
        raise UsbLinkLost(f"{watcher.result()} while waiting for WDA; choosing the link again")
    finally:
        watcher.cancel()
        opener.cancel()
        await asyncio.gather(watcher, opener, return_exceptions=True)


def _connection_lost(error):
    if isinstance(error, BaseExceptionGroup):
        return any(_connection_lost(child) for child in error.exceptions)
    return isinstance(error, (ClientConnectionError, ConnectionError))


def _describe(error):
    """``Type: message``, or only the type of an error that has no message."""
    text = str(error)
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def _keep_cleanup_failure(failure, cleanup_failure):
    """Leave ``failure`` as the error callers see, carrying the cleanup failure with it."""
    failure.cleanup_failure = cleanup_failure
    failure.add_note("Cleanup after this error also failed:\n" +
                     "".join(traceback.format_exception(cleanup_failure, chain=False)))


async def run_controller(args, hold=None, first_connect_timeout=None):
    """Connect, then hand the host to ``hold`` (MCP mode) or serve HTTP until stopped.

    ``first_connect_timeout`` shortens the first connect only; reconnects get the full startup timeout.
    """
    from .keepawake import DEFAULT_IDLE_LIMIT, ActivityKeepAwake
    from .network import NetworkBinding, BoundWDAClient, DeviceLockedError, is_recoverable_error
    timeout = getattr(args, "startup_timeout", 120.0)
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Startup timeout must be finite and positive")
    idle_timeout = getattr(args, "idle_timeout", DEFAULT_IDLE_LIMIT) or None  # 0 never times out.
    fallback = bool(getattr(args, "network_fallback", False))
    saved_runner_network = bool(args.paired_network or fallback)
    network_mode = bool(args.wda_url or saved_runner_network)
    udid = args.udid
    if network_mode and not udid:
        raise ValueError("An explicit hardware UDID is required for network mode")
    if not udid:
        devices = await DeviceServices.discover()
        if len(devices) != 1:
            raise RuntimeError("Connect exactly one USB iPhone or pass --udid; run iphone-agent doctor")
        udid = devices[0]["udid"]
    bundle_id = args.runner_bundle_id
    config = {}
    config_path = args.home / "runner.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        uses_saved_product = bool(config.get("appPath")) and not args.wda_url
        if config.get("udid") != udid and (bundle_id is None or saved_runner_network or uses_saved_product):
            raise SetupError("Saved runner belongs to a different iPhone; select --runner-bundle-id explicitly")
        if bundle_id is None:
            bundle_id = config["bundleId"]
        elif (saved_runner_network or uses_saved_product) and config.get("bundleId") != bundle_id:
            raise SetupError("Saved appPath belongs to a different runner bundle")
    if network_mode and not bundle_id:
        raise ValueError("Network mode needs --runner-bundle-id or an installed runner.json to verify the WDA identity")
    if saved_runner_network and not config.get("appPath"):
        raise SetupError("Paired-network startup needs the signed appPath saved by setup")
    from . import housekeeping
    tidy_state_directory = housekeeping.tidy_state_directory
    DEFAULT_KEEP_RECORDINGS_DAYS = housekeeping.DEFAULT_KEEP_RECORDINGS_DAYS
    from .locks import PhoneLock
    keep_recordings_days = getattr(args, "keep_recordings_days", DEFAULT_KEEP_RECORDINGS_DAYS)
    audit = None
    current = None
    pending = []
    host = None
    failure = None

    def summary(errors):
        return [{"type": type(error).__name__, "outcome": "unknown"} for error in errors]

    async def close_session(session):
        try:
            errors = await session.close()
        except BaseException as exc:
            if session not in pending:
                pending.append(session)
            if audit is not None:
                audit.append("connection_cleanup", device=udid, outcome="unknown", errors=summary([exc]))
            raise
        if errors and session not in pending:
            pending.append(session)
        elif not errors and session in pending:
            pending.remove(session)
        if audit is not None:
            audit.append("connection_cleanup", device=udid,
                         outcome="unknown" if errors else "attempts_completed", errors=summary(errors))
        return errors

    async def configure(session, deadline, fresh):
        nonlocal audit
        binding = None
        # Fallback picks the cable whenever it is attached, so each reconnect follows the current link.
        paired = args.paired_network or (fallback and not await _usb_present(udid))
        if paired:
            binding = await NetworkBinding.for_device(udid, args.wda_port)
        elif args.wda_url:
            binding = NetworkBinding(udid, args.wda_url)
            if fresh:
                resolved = await NetworkBinding.for_device(udid, binding.port)
                scheme = args.wda_url.split(":", 1)[0]
                address = f"[{resolved.host}]" if ":" in resolved.host else resolved.host
                binding = NetworkBinding(udid, f"{scheme}://{address}:{binding.port}")
            await binding.validate()
        session.binding = binding
        session.device = DeviceServices(udid)
        session.forward = binding.connection_factory(udid, binding.port) if binding else USBForward(udid, args.wda_port)
        if not binding:
            await session.device.info()
            if bundle_id and not config.get("appPath"):
                from .runner import WDARunner
                session.runner = WDARunner(udid, bundle_id, args.wda_port)
                await session.runner.open(timeout=max(0.001, deadline - asyncio.get_running_loop().time()))
        await session.forward.open()
        session.wda = BoundWDAClient(binding) if binding else WDAClient(f"http://127.0.0.1:{session.forward.port}")
        await session.wda.open()
        if (binding and paired) or (not binding and bundle_id and config.get("appPath")):
            from .apple_runner import AppleWDARunner
            session.runner = AppleWDARunner(udid, bundle_id, config["appPath"], session.wda,
                                           home=args.home, bind_host=binding.host if binding else "127.0.0.1",
                                           port=binding.port if binding else args.wda_port)
            opening = session.runner.open(timeout=max(0.001, deadline - asyncio.get_running_loop().time()))
            await (_open_while_usb_present(opening, udid) if fallback and not binding else opening)
        status = await session.wda.call("GET", "/status")
        if binding and (not isinstance(status, dict) or status.get("ready") is not True
                        or status.get("build", {}).get("productBundleIdentifier") != bundle_id.removesuffix(".xctrunner")):
            raise RuntimeError("Network WDA identity/readiness does not match the selected runner")
        if audit is None:
            audit = AuditLog(args.home / "audit.jsonl")
            if audit.set_aside is not None:
                _log.warning("The audit journal was full or unreadable; kept as %s and a new one started", audit.set_aside)
        session.wda.on_session_replaced = lambda ended: audit.append("wda_session_replaced", device=udid, session=ended)
        controller = session.controller = Controller(session.device, session.wda, audit)
        controller.transport = "paired-network" if binding else "USB"
        if not args.no_keep_awake:
            # No setting on the phone changes, so there is nothing to wait for and nothing to undo.
            controller.keep_awake = await ActivityKeepAwake(
                session.wda, idle_limit=idle_timeout,
                busy=lambda: controller.media is not None and controller.media.busy).open()
            audit.append("keep_awake", device=udid, operation="start", status=controller.keep_awake.status)
        controller.allow_erase = args.allow_erase
        if args.native_rsd_host:
            if not args.native_width or not args.native_height:
                raise ValueError("Native mode requires --native-width and --native-height in screen points")
            from .native import NativeClient
            controller.native = NativeClient.from_endpoint(args.native_rsd_host, args.native_rsd_port, udid=udid,
                                                           width_points=args.native_width, height_points=args.native_height)
            await controller.native.open()
        if args.sms_backup_dir:
            from .sms import MessageStore
            controller.messages = MessageStore(udid, args.sms_backup_dir)
        from .media import MediaManager
        controller.media = MediaManager(session.wda, udid, args.home / "recordings",
                                        connection_factory=binding.connection_factory if binding else None)

    async def connect(deadline, fresh=False):
        cleanup_errors = []
        last_recoverable = None  # Names the wait reason when the deadline cuts an attempt.
        locked_since = None
        while True:
            session = _OwnedSession()
            startup = asyncio.timeout_at(deadline)
            try:
                async with startup:
                    await configure(session, deadline, fresh)
                return session, cleanup_errors
            except BaseException as original:
                try:
                    errors = await close_session(session)
                except BaseException as cleanup_error:
                    raise BaseExceptionGroup("Connection startup and cleanup interrupted", [original, cleanup_error]) from original
                cleanup_errors.extend(errors)
                failed = BaseExceptionGroup("Connection startup and cleanup failed", [original, *errors]) if errors else original
                now = asyncio.get_running_loop().time()
                if any(isinstance(leaf, DeviceLockedError) for leaf in _leaves(original)):
                    locked_since = now if locked_since is None else locked_since
                    if now - locked_since >= LOCKED_CONNECT_GRACE:
                        raise PhoneLocked("The iPhone is locked, so the controller cannot start on it. Unlock it "
                                          "with Face ID or its passcode, then retry the call.") from original
                if is_recoverable_error(failed) and asyncio.get_running_loop().time() < deadline:
                    last_recoverable = original
                    # The audit journal opens only after a verified start, so retries are reported here.
                    _log.warning("iPhone startup attempt failed, retrying: %s", _describe(original))
                    await asyncio.sleep(min(STARTUP_POLL_INTERVAL, deadline - asyncio.get_running_loop().time()))
                    fresh = True
                    continue
                if is_recoverable_error(failed):
                    expired = TimeoutError(f"Startup did not reach a ready connection within {timeout:g}s")
                    if errors:
                        raise BaseExceptionGroup("Connection deadline and cleanup failed", [expired, original, *errors]) from original
                    raise expired from original
                if errors:
                    raise BaseExceptionGroup("Connection startup and cleanup failed", [original, *errors]) from original
                if isinstance(original, TimeoutError) and startup.expired():
                    raise TimeoutError(f"Startup did not reach a ready connection within {timeout:g}s") from (
                        last_recoverable or original)
                raise

    def attach_channels(session):
        from .control import ChannelController
        session.controller.channels = ChannelController(host)
        session.controller.media.datachannel_handler = session.controller.channels.attach

    async def reconnect():
        nonlocal current
        deadline = asyncio.get_running_loop().time() + timeout
        errors = await close_session(current)
        current.controller.connection_status = {"status": "disconnected"}
        session, startup_errors = await connect(deadline, fresh=True)
        current = session
        attach_channels(session)
        return session.controller, summary([*errors, *startup_errors])

    async def close_sessions():
        errors = []
        sessions = ([current] if current is not None else []) + [session for session in pending if session is not current]
        for session in sessions:
            try:
                errors.extend(await close_session(session))
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("Device session cleanup failed", errors)

    first_deadline = asyncio.get_running_loop().time() + (first_connect_timeout or timeout)
    # Held until every connection is closed, so reconnects stay inside it. A second controller of this
    # phone, from any state directory, is refused instead of silently taking over its WDA session.
    serving_url = f"http://{args.host}:{args.port}" if args.command == "serve" else None
    phone_lock = PhoneLock(udid, mode=args.command, home=args.home, url=serving_url,
                           idle_timeout=getattr(args, "idle_timeout", None) if args.command == "mcp" else None)
    await phone_lock.acquire(max(0.0, first_deadline - asyncio.get_running_loop().time()))
    try:
        current, initial_cleanup = await connect(first_deadline)
        # The open journal is exclusive to this controller, so no other one is using this state directory.
        # This runs after the connect so a large removal cannot use up its deadline.
        tidy_state_directory(args.home, keep_recordings_days, audit)
        current.controller.stop_requested = asyncio.Event() if args.command != "mcp" else None
        def recoverable(error):
            # In fallback mode a lost USB WDA link usually means the cable came out;
            # reconnecting chooses the link again, so it moves to the paired network.
            return is_recoverable_error(error) or (
                fallback and current is not None and current.binding is None and _connection_lost(error))

        host = ControllerHost(current.controller, reconnect, close_sessions, recoverable)
        current.controller.connection_status["previousCleanupErrors"] = summary(initial_cleanup)
        attach_channels(current)
        if args.command == "mcp":
            await (hold or run_mcp)(host)
        else:
            from .server import create_app
            runner = web.AppRunner(create_app(host, read_token(args.home)))
            await runner.setup()
            try:
                await web.TCPSite(runner, args.host, args.port).start()
                print(json.dumps({"status": "ready", "url": f"http://{args.host}:{args.port}",
                                  "tokenFile": str(args.home / "token"), "udid": udid, "transport": host.transport}), flush=True)
                await host.stop_requested.wait()
            finally:
                await runner.cleanup()
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            phone_lock.mark_cleaning()
        except OSError as exc:
            _log.warning("Could not mark the phone lock as closing: %s", exc)
        try:
            if host is not None:
                await host.close()
            else:
                try:
                    await close_sessions()
                finally:
                    if audit is not None:
                        audit.close()
        except BaseException as cleanup_failure:
            if failure is None:
                raise
            _keep_cleanup_failure(failure, cleanup_failure)
        finally:
            phone_lock.release()


MCP_INSTRUCTIONS = (
    "Controls a real iPhone. The first tool call connects, which keeps the phone awake and reserves it "
    "for this session. When the task that needed the phone is done, call iphone_disconnect once, after "
    "the last phone action, so the automation on the phone stops and the phone is free again."
)


def create_mcp(controller):
    from typing import Literal
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ImageContent
    server = MCPServer("iphone-agent", instructions=MCP_INSTRUCTIONS)

    async def execute(action, params=None):
        # The SDK hides the text of other exceptions; report it as the HTTP endpoint does.
        try:
            return await controller.execute(action, params)
        except Exception as exc:
            raise ToolError(_describe(exc)) from exc

    @server.tool()
    async def iphone_capabilities() -> dict:
        """Connect to the iPhone if this session has not yet, and report the link (USB or paired
        network), whether the phone is locked, keep-awake status and every action name that
        iphone_action accepts. The first call of a session connects, which takes 10 to 60 seconds.
        A locked phone must be unlocked by its owner; this server cannot enter Face ID or a passcode.
        """
        return await execute("capabilities")

    @server.tool()
    async def iphone_state() -> dict:
        """Read the foreground app's accessibility tree: each element's text, identifier and
        boundsInScreen in screen points, plus the foreground app and keyboard state. To tap an
        element, pass the centre of its bounds to iphone_tap.
        """
        return await execute("state")

    @server.tool()
    async def iphone_screenshot(scale: float = 1.0) -> list[ImageContent]:
        """See the iPhone screen as a PNG. At the default scale 1, a pixel position in the image is
        the screen-point position that iphone_tap and iphone_swipe take. For more detail pass scale
        2, then divide image coordinates by 2 before tapping. A scale at or above the screen's own
        pixel ratio returns its native pixels.
        """
        return [ImageContent(type="image", data=await execute("screenshot", {"scale": scale}), mimeType="image/png")]

    @server.tool()
    async def iphone_tap(x: float, y: float, hold_ms: int = 0) -> dict:
        """Tap the screen at x, y in screen points (origin top left). Set hold_ms for a long press."""
        if hold_ms > 0:
            return {"result": await execute("long_press", {"x": x, "y": y, "durationMs": hold_ms})}
        return {"result": await execute("tap", {"x": x, "y": y})}

    @server.tool()
    async def iphone_swipe(x1: float, y1: float, x2: float, y2: float, duration_ms: int = 300) -> dict:
        """Drag from x1, y1 to x2, y2 in screen points. To scroll a list down, swipe from a lower
        point to a higher one.
        """
        return {"result": await execute("swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "durationMs": duration_ms})}

    @server.tool()
    async def iphone_type(text: str, clear: bool = False) -> dict:
        """Type text into the focused field; tap the field first so the keyboard is up. clear empties
        the field before typing. A trailing newline presses return.
        """
        return {"result": await execute("type_text", {"text": text, "clear": clear})}

    @server.tool()
    async def iphone_press(button: Literal["home", "back", "app_switch", "lock", "unlock", "volumeup", "volumedown"]) -> dict:
        """Press a system button. home returns to the Home screen, back swipes back in the current
        app, app_switch opens the app switcher. unlock only wakes the screen: a passcode or Face ID
        still needs the owner.
        """
        if button in ("volumeup", "volumedown"):
            return {"result": await execute("key", {"name": button})}
        return {"result": await execute("global_action", {"name": button})}

    @server.tool()
    async def iphone_open(bundle_id: str | None = None, url: str | None = None) -> dict:
        """Open an app by bundle id (for example com.apple.Preferences), or open a URL or deep link.
        With both, the URL opens in that app. iphone_action packages lists installed bundle ids over USB.
        """
        if url is not None:
            return {"result": await execute("deep_link", {"url": url, **({"bundleId": bundle_id} if bundle_id else {})})}
        if bundle_id is None:
            raise ToolError("iphone_open needs bundle_id, url or both")
        return {"result": await execute("launch_app", {"bundleId": bundle_id})}

    @server.tool()
    async def iphone_action(action: str, params: dict | None = None) -> dict:
        """Run any other controller action by name with a params object. iphone_capabilities lists
        the names available now. Common ones: stop_app {bundleId}, tap_element {identifier},
        clipboard_get {}, clipboard_set {text}, location_set {latitude, longitude}, location_reset {},
        packages {}, files_list/files_read/files_write/files_delete {path, bundleId},
        recording_start/recording_stop {}, reconnect {}. A missing parameter returns an error that
        names it.
        """
        return {"result": await execute(action, params)}

    @server.tool()
    async def iphone_disconnect() -> dict:
        """Close the connection to the iPhone when your task is done: call it once, after your last
        phone action. It stops the automation running on the phone, lets the screen sleep and frees
        the phone for other sessions. A later tool call connects again. A running recording or
        stream must be stopped first (iphone_action recording_stop).
        """
        disconnect = getattr(controller, "disconnect", None)
        if disconnect is None:
            raise ToolError("This server keeps its connection for its whole life; stop the server to close it.")
        try:
            return await disconnect()
        except Exception as exc:
            raise ToolError(_describe(exc)) from exc

    return server


async def run_mcp(controller):
    await create_mcp(controller).run_stdio_async()


def _leaves(error):
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            yield from _leaves(child)
    else:
        yield error


class DeferredHost:
    """Answer MCP clients without a phone and connect on the first tool call.

    Clients start this server with every session, so connecting at startup made it
    exit before the handshake while unplugged and let idle sessions take the phone.
    A failed connection is that call's error; the next call starts a fresh one.
    """

    def __init__(self, args, start=None, on_close=None):
        self._args = args
        self._start = start or run_controller
        self._on_close = on_close
        self._lock = asyncio.Lock()
        self._task = None
        self._ready = None
        self._release = None
        self._reported = None
        self._closing = None
        idle = getattr(args, "idle_timeout", None)
        self._idle_timeout = (idle if isinstance(idle, (int, float)) and not isinstance(idle, bool)
                              and math.isfinite(idle) and idle > 0 else None)
        self._idle_task = None
        self._connection_started = asyncio.Event()
        self._calls = 0
        self._last_call = None

    async def execute(self, action, params=None):
        self._calls += 1
        try:
            return await (await self._ensure()).execute(action, params)
        finally:
            self._calls -= 1
            self._last_call = asyncio.get_running_loop().time()

    async def _ensure(self):
        async with self._lock:
            if self._closing is not None:
                raise ConnectionUnavailable("iPhone controller is shutting down")
            if self._task is not None and not self._task.done() and self._release.is_set():
                # A disconnect left this connection closing (its caller may have given up waiting); never reuse it.
                await asyncio.wait({self._task})
                self._connection_started.clear()
                if not self._task.cancelled() and self._task.exception() is not None:
                    _log.warning("Disconnect cleanup failed: %s", _describe(self._task.exception()))
                if self._closing is not None:  # Shutdown began while this call waited.
                    raise ConnectionUnavailable("iPhone controller is shutting down")
            if self._task is None or self._task.done():
                if self._task is not None and not self._task.cancelled():
                    self._task.exception()  # Retrieved; an abandoned call may never have seen it.
                self._ready = asyncio.get_running_loop().create_future()
                ready = self._ready
                release = self._release = asyncio.Event()

                async def hold(host):
                    ready.set_result(host)
                    await release.wait()

                self._task = asyncio.create_task(self._start(self._args, hold=hold),
                                                 name="iphone-agent-connect")
                self._connection_started.set()
                if self._idle_timeout is not None and self._idle_task is None:
                    self._idle_task = asyncio.create_task(self._disconnect_when_idle(), name="iphone-agent-idle")
            # Waiting never cancels the startup task; an abandoned call leaves it for the next one.
            await asyncio.wait({self._ready, self._task}, return_when=asyncio.FIRST_COMPLETED)
            if self._ready.done():
                return self._ready.result()
            if self._task.cancelled():
                raise ConnectionUnavailable("iPhone connection startup was cancelled; retry the call")
            failure = self._reported = self._task.exception()
            leaves = list(_leaves(failure))  # A group lists the startup error first, then its cleanup errors.
            for leaf in leaves:
                if isinstance(leaf, (ConnectionUnavailable, PhoneLocked)):
                    raise leaf  # Already says what is wrong and what to do.
                if isinstance(leaf, AuditLogBusy):
                    raise ConnectionUnavailable(str(leaf)) from failure
            cause = leaves[0]
            detail = _describe(cause)
            # asyncio.timeout raises its TimeoutError from the CancelledError it used, which says nothing.
            if isinstance(cause, TimeoutError) and not isinstance(cause.__cause__, (asyncio.CancelledError, type(None))):
                detail += f"; last error {_describe(cause.__cause__)}"
            from .apple_runner import AppleRunnerError
            if isinstance(cause, (SetupError, ValueError, AppleRunnerError)):
                # A setup or runner failure: the link advice below would send the caller the wrong way.
                raise ConnectionUnavailable(f"iPhone controller could not start ({detail}). Retry the call; if it "
                                            f"repeats, see {self._args.home / 'mcp-session.log'}.") from failure
            wireless = getattr(self._args, "paired_network", False) or getattr(self._args, "wda_url", None)
            if getattr(self._args, "network_fallback", False):
                advice = "Connect it by USB or keep it on this Mac's network, unlock it"
            else:
                advice = "Keep it on this Mac's network, unlock it" if wireless else "Connect it by USB, unlock it"
            raise ConnectionUnavailable(f"iPhone is not available ({detail}). {advice}, then retry the call.") from failure

    def _busy(self):
        """A recording or stream is running; it makes no calls, and a disconnect would end it."""
        host = self._ready.result() if self._ready is not None and self._ready.done() and not self._ready.cancelled() else None
        return bool(getattr(getattr(host, "media", None), "busy", False))

    async def _disconnect_when_idle(self):
        """Close a connection no call has used for the idle timeout; the next call opens a new one.

        A connected session keeps the phone awake and keeps other sessions out, and clients
        leave their MCP servers running for days.
        """
        loop = asyncio.get_running_loop()
        while True:
            await self._connection_started.wait()
            last = loop.time() if self._last_call is None else self._last_call
            remaining = last + self._idle_timeout - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            async with self._lock:
                task = self._task
                if task is None or task.done():
                    self._connection_started.clear()  # Nothing is connected until the next call.
                    continue
                if (self._closing is None and self._ready.done() and not self._calls and self._last_call is not None
                        and loop.time() - self._last_call >= self._idle_timeout and not self._busy()):
                    _log.warning("No tool call for %gs; disconnecting. The next call connects again.", self._idle_timeout)
                    failure = await self._end_connection(task)
                    if failure is not None:
                        _log.warning("Idle disconnect cleanup failed: %s", _describe(failure))
                    continue
            # A call is running, the connection is still starting, or a recording is in progress.
            await asyncio.sleep(IDLE_RECHECK_INTERVAL)

    async def _end_connection(self, task):
        """Release a served connection and wait for its cleanup; the lock is held. Returns a cleanup failure."""
        self._release.set()
        # Not gather: cancelling the caller must not cancel the session's cleanup.
        await asyncio.wait({task})
        self._connection_started.clear()  # Nothing is connected until the next call.
        return None if task.cancelled() else task.exception()

    async def disconnect(self):
        """Close the connection now, as the idle timeout would; the next call connects again."""
        async with self._lock:
            if self._closing is not None:
                raise ConnectionUnavailable("iPhone controller is shutting down")
            task = self._task
            if task is None or task.done():
                return {"disconnected": False, "detail": "No iPhone connection was open."}
            if self._calls:
                raise ConnectionUnavailable("Another iPhone tool call is still running. Wait for it to finish, "
                                            "then call iphone_disconnect again.")
            if self._busy():
                raise ConnectionUnavailable("A recording or stream is running. Stop it first "
                                            "(iphone_action recording_stop), then call iphone_disconnect again.")
            if not self._ready.done():
                task.cancel()  # Still connecting: nothing was served, so stop the startup.
            failure = await self._end_connection(task)
            self._task = None  # Its outcome is reported here; close() must not raise it again.
        if failure is not None:
            raise ConnectionUnavailable(f"The connection closed, but its cleanup failed: {_describe(failure)}")
        _log.warning("Disconnected on request. The next call connects again.")
        return {"disconnected": True, "detail": "The iPhone connection is closed; the next tool call connects again."}

    def close(self):
        """Release the session so run_controller closes the connection; callers share one cleanup."""
        if self._closing is None:
            if self._on_close is not None:
                self._on_close()
            self._closing = asyncio.ensure_future(self._close())
        return asyncio.shield(self._closing)

    async def _close(self):
        if self._idle_task is not None:
            self._idle_task.cancel()
            await asyncio.gather(self._idle_task, return_exceptions=True)
        task = self._task
        if task is None:
            return
        self._release.set()
        served = self._ready.done()
        if not served:
            task.cancel()
        result, = await asyncio.gather(task, return_exceptions=True)
        failures = self._unreported(result)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("iPhone session cleanup failed", failures)

    def _unreported(self, result):
        """What no caller was told about in how the startup task ended."""
        cleanup_failure = getattr(result, "cleanup_failure", None)
        if isinstance(result, BaseExceptionGroup):
            # Keep the cleanup errors of a cancelled startup. A call's error names only the first error of a group.
            result = result.split(asyncio.CancelledError)[1]
        elif result is self._reported or isinstance(result, asyncio.CancelledError):
            result = None
        return [failure for failure in (result, cleanup_failure) if failure is not None]


async def run_mcp_deferred(args, serve=run_mcp, exit=os._exit, host=None):
    import signal
    host = host or DeferredHost(args)
    loop = asyncio.get_running_loop()
    terminating = []

    async def clean_up_and_exit():
        code = 0
        try:
            await host.close()
        except BaseException as exc:
            code = 1
            with contextlib.suppress(OSError, ValueError):
                traceback.print_exception(exc, file=sys.stderr)
        with contextlib.suppress(OSError, ValueError):
            sys.stderr.flush()
        exit(code)

    def terminate():
        # Clients stop stdio servers with SIGTERM and may SIGKILL soon after. The SDK
        # reads stdin in a thread that cancellation cannot interrupt, so close the
        # connection now and exit instead of waiting for stdin EOF.
        loop.add_signal_handler(signal.SIGTERM, lambda: None)
        terminating.append(asyncio.ensure_future(clean_up_and_exit()))

    loop.add_signal_handler(signal.SIGTERM, terminate)
    try:
        await serve(host)
    finally:
        try:
            await host.close()
        finally:
            loop.remove_signal_handler(signal.SIGTERM)


async def run(args):
    if args.command == "devices":
        print(json.dumps(await DeviceServices.discover()))
    elif args.command == "doctor":
        result = await doctor()
        print(json.dumps(result, indent=2))
        return 0 if result["usbReady"] else 2
    elif args.command == "audit":
        records = AuditLog.verify(args.home / "audit.jsonl")
        starts = {r["request_id"] for r in records if r["event"] == "request"}
        ends = {r["request_id"] for r in records if r["event"] == "result"}
        print(json.dumps({"valid": True, "records": len(records), "unresolved": sorted(starts - ends)}))
    elif args.command == "call":
        async with ClientSession() as client:
            async with client.post(args.url + "/execute",
                                   headers={"Authorization": "Bearer " + read_token(args.home)},
                                   json={"action": args.action, "params": json.loads(args.params)}) as response:
                print(await response.text())
                return 0 if response.status == 200 else 1
    elif args.command == "setup":
        from .setup import build_runner
        return await build_runner(args)
    else:
        await run_controller(args)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Audited USB iPhone controller")
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("devices", "doctor", "audit"):
        sub.add_parser(name)
    for name in ("serve", "mcp"):
        command = sub.add_parser(name)
        command.add_argument("--udid")
        command.add_argument("--wda-port", type=int, default=8100)
        connection = command.add_mutually_exclusive_group()
        connection.add_argument("--wda-url", help="Existing WDA origin at the selected phone's current CoreDevice tunnel IP")
        connection.add_argument("--paired-network", action="store_true", help="Start the saved signed runner through Apple's paired CoreDevice network path")
        connection.add_argument("--network-fallback", action="store_true",
                                help="Use USB while the cable is attached, otherwise --paired-network; chosen again at each connection")
        command.add_argument("--runner-bundle-id")
        command.add_argument("--startup-timeout", type=float, default=120, help="Seconds to wait for the signed runner to answer")
        command.add_argument("--no-keep-awake", action="store_true", help="Send no keep-awake input; the phone follows its Auto-Lock setting")
        command.add_argument("--idle-timeout", type=_idle_seconds, default=1800.0,
                             help="Seconds after the last agent call until keep-awake stops; an MCP session also "
                                  "disconnects. 0 never times out")
        command.add_argument("--keep-recordings-days", type=_keep_days, default=30.0,
                             help="Days to keep finished recordings in the state directory; older ones are removed "
                                  "once the controller has connected. 0 keeps every recording")
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--port", type=int, default=8742)
        command.add_argument("--native-rsd-host")
        command.add_argument("--native-rsd-port", type=int, default=58783)
        command.add_argument("--native-width", type=float)
        command.add_argument("--native-height", type=float)
        command.add_argument("--sms-backup-dir", type=Path)
        command.add_argument("--allow-erase", action="store_true")
        if name == "mcp":
            command.add_argument("--session-worker", type=int, help=argparse.SUPPRESS)
    call = sub.add_parser("call")
    call.add_argument("action")
    call.add_argument("params", nargs="?", default="{}")
    call.add_argument("--url", default="http://127.0.0.1:8742")
    setup = sub.add_parser("setup")
    setup.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2] / "src/github/WebDriverAgent")
    setup.add_argument("--team", required=True)
    setup.add_argument("--bundle-id", default="org.iphoneagent.WebDriverAgentRunner")
    setup.add_argument("--udid")
    setup.add_argument("--tunnel", action="store_true")
    setup.add_argument("--headless-assets", action="store_true")
    setup.add_argument("--build-only", action="store_true")
    setup.add_argument("--paired-network", action="store_true", help="Build and install using the selected paired CoreDevice phone")
    setup.add_argument("--authentication-key-path", type=Path)
    setup.add_argument("--authentication-key-id")
    setup.add_argument("--authentication-key-issuer-id")
    args = parser.parse_args()
    if args.command == "mcp":
        from .mcp_session import run_front, run_worker
        (run_front if args.session_worker is None else run_worker)(args)
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        code = 0
    except Exception as exc:
        print(_describe(exc), file=sys.stderr)
        code = 1
    raise SystemExit(code)
