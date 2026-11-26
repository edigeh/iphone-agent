import asyncio
import base64
import uuid
from contextlib import nullcontext
from . import __version__
from .audit import digest, json_safe
from .wda import WDAError


RPC_ACTIONS = {
    "portal.Tap": "tap", "portal.Swipe": "swipe", "portal.Global": "global_action",
    "portal.App": "launch_app", "portal.Install": "install", "portal.Packages": "packages",
    "portal.Screenshot": "screenshot", "portal.State": "state", "portal.Version": "version",
    "portal.Time": "time", "portal.Reset": "reset", "portal.KeyboardInput": "type_text",
    "portal.KeyboardClear": "clear_text", "portal.KeyboardKey": "key",
    "keyboard.Input": "type_text", "keyboard.Clear": "clear_text", "keyboard.Key": "key",
    "app.Stop": "stop_app", "app.DeepLink": "deep_link", "app.Uninstall": "uninstall", "app.Reset": "app_reset",
    "files.List": "files_list", "files.Fetch": "files_fetch", "files.Push": "files_push", "files.Delete": "files_delete",
    "timezone.Get": "timezone_get", "timezone.Set": "timezone_set",
    "location.Get": "location_get", "location.Set": "location_set", "location.Reset": "location_reset",
    "clipboard.Get": "clipboard_get", "clipboard.Set": "clipboard_set",
    "sockstun.Connect": "proxy_connect", "sockstun.Disconnect": "proxy_disconnect", "sockstun.Stats": "proxy_stats",
    "webrtc.Offer": "webrtc_offer", "webrtc.Ice": "webrtc_ice", "webrtc.Keepalive": "webrtc_keepalive",
    "webrtc.Requestframe": "webrtc_requestframe", "webrtc.Rtcconfiguration": "webrtc_configuration",
    "sms.Get": "sms_get", "sms.List": "sms_list", "sms.Subscriptions": "sms_subscriptions",
    "sms.Send": "sms_send", "sms.Sendstatus": "sms_sendstatus",
}

USB_ACTIONS = {"info", "packages", "time", "timezone_get", "timezone_set", "install", "uninstall",
               "files_list", "files_read", "files_write", "files_fetch", "files_push", "files_delete",
               "sms_refresh", "sms_watch", "reset"}


class Unsupported(RuntimeError):
    pass


class ConnectionUnavailable(RuntimeError):
    pass


class PhoneLocked(RuntimeError):
    pass


# Input on a locked phone lands on the lock screen, so these are refused with the reason instead.
INPUT_ACTIONS = frozenset({"tap", "swipe", "long_press", "touch_actions", "tap_element", "type_text", "clear_text",
                           "key", "launch_app", "deep_link"})

# What a caller that left a parameter out is told.
USAGE = {
    "tap": "tap takes x and y in screen points",
    "long_press": "long_press takes x and y in screen points and an optional durationMs",
    "swipe": "swipe takes x1, y1, x2, y2 in screen points and an optional durationMs",
    "tap_element": "tap_element takes the accessibility identifier of one element",
    "touch_actions": "touch_actions takes actions, a list of pointerDown, pointerMove, pause and pointerUp items",
    "type_text": "type_text takes text and an optional clear boolean",
    "key": "key takes name (home, volumeup, volumedown) or an Android-style key_code",
    "global_action": "global_action takes name: home, back, app_switch, lock or unlock",
    "launch_app": "launch_app takes bundleId",
    "stop_app": "stop_app takes bundleId",
    "deep_link": "deep_link takes url and an optional bundleId",
    "clipboard_set": "clipboard_set takes text",
    "location_set": "location_set takes latitude and longitude",
    "timezone_set": "timezone_set takes timeZone",
    "screenshot": "screenshot takes an optional scale, the number of image pixels per screen point",
}
SCREENSHOT_SCALE_MIN = 0.25
# Input reuses an unlocked reading this fresh instead of asking again; keep-awake, when on, takes one
# every 10 seconds. Input sent in that window after a lock lands on the lock screen.
LOCK_READING_MAX_AGE = 12.0
ACTION_TIMEOUT = 120.0  # Seconds one action may take; reconnect has its own startup deadline.


def scale_screenshot(encoded, pixels_per_point, scale):
    """Resize a base64 PNG so one screen point covers ``scale`` pixels."""
    import io
    from PIL import Image
    with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as image:
        width = max(1, round(image.width / pixels_per_point * scale))
        height = max(1, round(image.height / pixels_per_point * scale))
        output = io.BytesIO()
        image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS).save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


class Controller:
    def __init__(self, device, wda, audit):
        self.device, self.wda, self.audit = device, wda, audit
        self.lock = asyncio.Lock()
        self.media = None
        self.native = None
        self.messages = None
        self.allow_erase = False
        self.channels = None
        self.transport = "USB"
        self.keep_awake = None
        self.stop_requested = None
        self.reconnect_handler = None
        self.connection_status = None
        self._pixels_per_point = None

    async def execute(self, action, params=None):
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        action = RPC_ACTIONS.get(action, action)
        if self.keep_awake is not None:
            self.keep_awake.activity()
        request_id = uuid.uuid4().hex
        async with (nullcontext() if action in {"sms_watch", "reconnect", "shutdown"} else self.lock):
            self.audit.append("request", request_id=request_id, action=action,
                              device=self.device.udid, params_sha256=digest(params))
            try:
                operation_timeout = None if action == "reconnect" else ACTION_TIMEOUT
                if action == "proxy_connect":
                    from .wda import proxy_connect_request_timeout
                    # Fresh network identity validation can use two 15s queries
                    # before the separately bounded HTTP request begins.
                    operation_timeout = proxy_connect_request_timeout(params.get("timeout", 30.0)) + 30.0
                limit = asyncio.timeout(operation_timeout)
                try:
                    async with limit:
                        result = json_safe(await self._dispatch(action, dict(params)))
                except TimeoutError as exc:
                    if not limit.expired():
                        raise
                    raise TimeoutError(f"{action} did not finish within {operation_timeout:g} s; the phone or its "
                                       "runner may be hung. Retry the call, or call reconnect.") from exc
                except KeyError as exc:
                    key = exc.args[0] if exc.args else None
                    if action == "reconnect" or not isinstance(key, str) or key in params:
                        raise  # Not a parameter the caller left out.
                    usage = USAGE.get(action, f"see the action table in the README for {action}")
                    raise ValueError(f"Missing parameter {key!r}: {usage}.") from exc
            except BaseException as exc:
                self.audit.append("result", request_id=request_id, status="error",
                                  error_type=type(exc).__name__, error_sha256=digest(str(exc)))
                raise
            self.audit.append("result", request_id=request_id, status="ok", result_sha256=digest(result))
            return result

    async def _dispatch(self, action, p):
        if str(p.get("displayId", 0)) != "0":
            raise Unsupported("Only the primary iPhone display is supported")
        if action == "version":
            return __version__
        if action == "shutdown":
            if p:
                raise ValueError("Shutdown takes no parameters")
            if self.stop_requested is None:
                raise Unsupported("Close the MCP client to stop its controller")
            self.stop_requested.set()
            return {"status": "stopping"}
        if action == "reconnect":
            if self.reconnect_handler is None:
                raise Unsupported("This controller has no managed reconnection lifecycle")
            if set(p) - {"reason"} or ("reason" in p and not isinstance(p["reason"], str)):
                raise ValueError("Reconnect accepts only an optional reason string")
            return await self.reconnect_handler()
        if self.connection_status and self.connection_status.get("status") != "ready" and action not in {"version", "capabilities"}:
            raise ConnectionUnavailable("Device connection is unavailable; call reconnect")
        gesture = action == "global_action" and p.get("name") not in ("lock", "unlock")  # home, back, app switcher
        if (action in INPUT_ACTIONS or gesture) and await self.wda.locked(max_age=LOCK_READING_MAX_AGE):
            raise PhoneLocked("The iPhone is locked, so input would land on the lock screen. Unlock it with Face ID "
                              "or its passcode, then retry. screenshot and state still work while it is locked.")
        if action == "screen_geometry":
            width, height = await self.wda._screen_size()
            return {"width": width, "height": height}
        if action == "tap_element":
            return await self.wda.click_element(p["identifier"], expected_value=p.get("expectedValue"),
                                                scroll_to_visible=p.get("scrollToVisible", True))
        if action in {"a11y_tree", "phone_state"}:
            state = await self.wda.state()
            if action not in state:
                raise RuntimeError(f"WDA state did not include {action}")
            return state[action]
        if action == "touch_actions":
            actions = p.get("actions")
            if not isinstance(actions, list) or not actions or len(actions) > 4096:
                raise ValueError("Provide 1–4096 touch actions")
            return await self.wda.call("POST", "/mobilerun/actions?scale=1", actions)
        if action == "capabilities":
            if self.connection_status and self.connection_status.get("status") != "ready":
                return {"device": self.device.udid, "transport": self.transport,
                        "implemented": ["capabilities", "reconnect", "version"] + (["shutdown"] if self.stop_requested else []),
                        "connection": dict(self.connection_status), "wdaStatus": None,
                        "keepAwake": self.keep_awake.status if self.keep_awake else {"active": False}}
            implemented = set(RPC_ACTIONS.values()) - {"reset", "app_reset", "sms_send", "sms_sendstatus", "sms_get", "sms_list", "sms_subscriptions", "proxy_connect", "proxy_disconnect", "proxy_stats"}
            implemented |= {"info", "a11y_tree", "phone_state", "screen_geometry", "touch_actions", "long_press", "files_read", "files_write", "tap_element"}
            if self.media is None:
                implemented = {a for a in implemented if not a.startswith("webrtc_")}
            else:
                implemented |= {"stream_video_start", "stream_video_list", "stream_video_get", "stream_video_stop", "stream_video_keyframe",
                                "stream_audio_start", "stream_audio_list", "stream_audio_get", "stream_audio_stop",
                                "stream_broadcast_start", "stream_broadcast_status", "stream_broadcast_stop",
                                "recording_start", "recording_status", "recording_stop", "webrtc_close"}
            unavailable = {"app_reset": "iOS backend has no app-data-reset service", "sms_send": "iOS backend is read-only"}
            if self.messages:
                implemented |= {"sms_get", "sms_list", "sms_refresh", "sms_watch", "sms_subscriptions"}
            else:
                unavailable["sms"] = "Enable a Messages backup source"
            if self.allow_erase:
                implemented.add("reset")
            else:
                unavailable["reset"] = "Requires separate erase authorization"
            native_report = None
            if self.native:
                native_report = self.native.capability_report()
                implemented.add("native_geometry")
                for action in {"tap", "swipe", "screenshot"}:
                    if action in native_report["unavailable"]:
                        implemented.discard(action)
                        unavailable[action] = native_report["unavailable"][action]
            if self.stop_requested is not None:
                implemented.add("shutdown")
            if self.reconnect_handler is not None:
                implemented.add("reconnect")
            conditional = {action: "Tunnel runner and Network Extension profile"
                           for action in ("proxy_connect", "proxy_disconnect", "proxy_stats")}
            conditional["audio"] = "ReplayKit broadcast consent"
            try:
                usb_present = await self.device.usb_present()
            except Exception:
                usb_present = False
            if not usb_present:
                for name in implemented & USB_ACTIONS:
                    conditional[name] = "Selected iPhone is not currently connected through USB"
                implemented -= USB_ACTIONS
            return {"backend": "hybrid" if self.native else "wda", "device": self.device.udid, "transport": self.transport, "coordinateUnits": "points",
                    "implemented": sorted(implemented),
                    "conditional": conditional, "usbPresent": bool(usb_present),
                    "locked": await self.wda.locked(),
                    "keepAwake": self.keep_awake.status if self.keep_awake else {"active": False},
                    "connection": self.connection_status or {"status": "ready"},
                    "unavailable": unavailable,
                    **({"native": native_report} if native_report is not None else {}),
                    "wdaStatus": await self.wda.call("GET", "/status")}
        if action == "screenshot" and p.get("scale") is not None:
            return await self._screenshot(p.pop("scale"), p)
        if self.native and action in {"tap", "swipe", "screenshot"}:
            return await self.native.dispatch(action, p)
        if action == "native_geometry" and self.native:
            self.native.set_geometry(p["width"], p["height"])
            return {"width": p["width"], "height": p["height"]}
        if action in ("info", "packages", "time", "timezone_get"):
            return await getattr(self.device, action)()
        if action == "timezone_set":
            return await self.device.timezone_set(p.get("timeZone", p.get("timezone")))
        if action == "install":
            if "path" in p:
                return await self.device.install(p["path"])
            import tempfile
            from pathlib import Path
            urls = p.get("urls", [])
            if not isinstance(urls, list) or not urls or len(urls) > 10:
                raise ValueError("Provide path or 1–10 IPA urls")
            for url in urls:
                data = await self._download(url, 256 * 1024 * 1024)
                with tempfile.TemporaryDirectory(prefix="iphone-agent-install-") as temp:
                    path = Path(temp) / "app.ipa"
                    path.write_bytes(data)
                    await self.device.install(str(path))
            return {"installed": len(urls)}
        if action == "uninstall":
            return await self.device.uninstall(p.get("bundleId", p.get("package", p.get("packageName"))))
        if action.startswith("files_"):
            path, bundle = p["path"], p.get("bundleId")
            if action == "files_fetch":
                data = await self._download(p["url"], 24 * 1024 * 1024)
                await self.device.files_write(path, data, bundle)
                return {"size": len(data)}
            if action == "files_push":
                from aiohttp import ClientSession, ClientTimeout
                self._check_url(p["url"])
                data = await self.device.files_read(path, bundle)
                async with ClientSession(timeout=ClientTimeout(total=60), trust_env=False) as client:
                    async with client.put(p["url"], data=data, allow_redirects=False) as response:
                        response.raise_for_status()
                        return {"size": len(data), "status": response.status}
            if action == "files_read":
                data = await self.device.files_read(path, bundle)
                return {"data": base64.b64encode(data).decode(), "encoding": "base64", "size": len(data)}
            if action == "files_write":
                data = base64.b64decode(p["data"], validate=True)
                return await self.device.files_write(path, data, bundle)
            if action in ("files_list", "files_delete"):
                return await getattr(self.device, action)(path, bundle)
        if action.startswith(("stream_", "recording_", "webrtc_")) and self.media:
            return await self.media.dispatch(action, p)
        if action.startswith("sms_"):
            if self.messages and action in {"sms_get", "sms_list", "sms_subscriptions", "sms_refresh", "sms_watch"}:
                return await self.messages.dispatch(action, p)
            raise Unsupported("Messages backup must be explicitly configured; iOS SMS sending is not provided")
        if action == "reset" and self.allow_erase:
            if p.get("confirmEraseUDID") != self.device.udid:
                raise ValueError("Device erase requires confirmEraseUDID matching the selected phone")
            return await self.device.erase()
        if action in ("reset", "app_reset"):
            raise Unsupported("Data reset requires a separately authorized erase or app reinstall")
        if action == "type_text" and "base64_text" in p:
            p["text"] = base64.b64decode(p["base64_text"], validate=True).decode("utf-8")
        if action == "global_action" and p.get("name") == "unlock":
            try:
                return await self.wda.dispatch(action, p)
            except WDAError:
                if await self.wda.locked():
                    raise PhoneLocked("The screen is awake, but the iPhone is still locked: only its owner can unlock it, "
                                      "with Face ID or its passcode.") from None
                return None  # Unlocked, which is what was asked; WDA stopped waiting just before Face ID finished.
        return await self.wda.dispatch(action, p)

    async def _screenshot(self, scale, p):
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not scale >= SCREENSHOT_SCALE_MIN:
            raise ValueError(f"screenshot scale must be a number of at least {SCREENSHOT_SCALE_MIN:g}")
        encoded = await (self.native.dispatch("screenshot", p) if self.native else self.wda.dispatch("screenshot", p))
        if self._pixels_per_point is None:
            # The ratio is fixed for a device, unlike the width, which changes with rotation.
            import io
            from PIL import Image
            width, height = await self.wda._screen_size()
            with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as image:
                self._pixels_per_point = max(image.width, image.height) / max(width, height)
        if scale >= self._pixels_per_point:
            return encoded  # The screen has no more detail than its native pixels.
        return await asyncio.to_thread(scale_screenshot, encoded, self._pixels_per_point, scale)

    @staticmethod
    def _check_url(url):
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Transfer URL must be HTTP(S) without embedded credentials")

    @classmethod
    async def _download(cls, url, limit):
        from aiohttp import ClientSession, ClientTimeout
        cls._check_url(url)
        async with ClientSession(timeout=ClientTimeout(total=90), trust_env=False) as client:
            async with client.get(url, allow_redirects=False) as response:
                response.raise_for_status()
                if response.status != 200:
                    raise ValueError("Download must return HTTP 200 without a redirect")
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ValueError("Download exceeds size limit")
                return bytes(data)

    async def close(self, *, close_audit=True, resource_timeout=None):
        errors = []
        cancelled = None
        for resource in (self.channels, self.media, self.keep_awake, self.native, self.messages, self.wda, self.device):
            if resource is None:
                continue
            try:
                async with asyncio.timeout(resource_timeout):
                    await resource.close()
                if resource is self.keep_awake:
                    self.audit.append("keep_awake", device=self.device.udid, operation="stop", status=resource.status)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except BaseException as exc:
                errors.append(exc)
        try:
            if close_audit:
                self.audit.close()
        except Exception as exc:
            errors.append(exc)
        if cancelled:
            if errors:
                raise BaseExceptionGroup("Controller cleanup cancelled with errors", [cancelled, *errors])
            raise cancelled
        if errors:
            raise BaseExceptionGroup("Controller cleanup failed", errors)


class ControllerHost:
    """Keep one audited host endpoint while replacing owned device sessions."""

    def __init__(self, controller, reconnect, close, recoverable):
        self.controller = controller
        self._reconnect_session, self._close_sessions, self._recoverable = reconnect, close, recoverable
        self._lock = asyncio.Lock()
        self._recovery_task = None
        self._active_tasks = set()
        self._closing = False
        self._generation = 1
        self._attach(controller, [])

    def __getattr__(self, name):
        return getattr(self.controller, name)

    def _attach(self, controller, cleanup_errors):
        self.controller = controller
        controller.reconnect_handler = self._reconnect
        controller.connection_status = {"status": "ready", "generation": self._generation,
                                        "previousCleanupErrors": cleanup_errors}

    async def _reconnect(self):
        if self._closing or (self.stop_requested is not None and self.stop_requested.is_set()):
            raise ConnectionUnavailable("Controller is stopping")
        task = asyncio.current_task()
        self._recovery_task = task
        previous = self.controller
        try:
            controller, cleanup_errors = await self._reconnect_session()
            controller.stop_requested = previous.stop_requested
            self._generation += 1
            self._attach(controller, cleanup_errors)
            return dict(controller.connection_status)
        except BaseException as exc:
            previous.connection_status = {"status": "disconnected", "generation": self._generation,
                                          "errorType": type(exc).__name__}
            raise
        finally:
            if self._recovery_task is task:
                self._recovery_task = None

    async def _automatic_reconnect(self):
        try:
            await self.execute("reconnect", {"reason": "transport_failure"})
        except asyncio.CancelledError:
            return
        except BaseException as exc:
            self.audit.append("connection_recovery", device=self.device.udid, status="error",
                              error_type=type(exc).__name__, error_sha256=digest(str(exc)))

    def _cancel_tasks(self):
        tasks = self._active_tasks | ({self._recovery_task} if self._recovery_task else set())
        for task in tasks:
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        return [task for task in tasks if task is not asyncio.current_task()]

    async def execute(self, action, params=None):
        action = RPC_ACTIONS.get(action, action)
        if action == "shutdown":
            target = self.controller
            result = await target.execute(action, params)
            self._closing = True
            self._cancel_tasks()
            return result
        async with (nullcontext() if action == "sms_watch" else self._lock):
            task = asyncio.current_task()
            self._active_tasks.add(task)
            try:
                status = self.controller.connection_status
                # sms_watch runs outside the host lock, and two reconnects at once would leave one
                # new session that nothing closes.
                if (action not in {"reconnect", "version", "sms_watch"} and status
                        and status.get("status") == "disconnected"):
                    # The last recovery failed; each call tries again instead of asking the caller to.
                    # A failure here is this call's error and starts no second recovery behind it.
                    await self.controller.execute("reconnect", {"reason": "previous_recovery_failed"})
                try:
                    return await self.controller.execute(action, params)
                except Exception as exc:
                    # Controller.execute has already committed the failed action's receipt.
                    # Recovery creates a new session; it never repeats that API action.
                    if (action != "reconnect" and not self._closing and self._recoverable(exc)
                            and (self._recovery_task is None or self._recovery_task.done())):
                        self._recovery_task = asyncio.create_task(self._automatic_reconnect(), name="iphone-agent-reconnect")
                    raise
            finally:
                self._active_tasks.discard(task)

    async def close(self):
        self._closing = True
        tasks = self._cancel_tasks()
        await asyncio.gather(*tasks, return_exceptions=True)
        errors = []
        try:
            await self._close_sessions()
        except BaseException as exc:
            errors.append(exc)
        try:
            self.audit.close()
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise BaseExceptionGroup("Controller host cleanup failed", errors)
