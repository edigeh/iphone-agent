"""Async client for the open WebDriverAgent iOS HTTP routes.

The fork's custom state and actions routes share a coordinate scale. This
client requests ``scale=1`` on both, so coordinates are iOS logical points.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import time
from typing import Any, Callable
from urllib.parse import quote

import aiohttp

from .axtree import home_icons, visible, walk

_log = logging.getLogger(__name__)


_HOME_MODAL_CLASSES = frozenset({"XCUIElementTypeAlert", "XCUIElementTypeSheet", "XCUIElementTypeKeyboard"})
MODIFIER_KEYCODES = frozenset({57, 58, 59, 60, 113, 114, 115, 117, 118})
# USB HID consumer page, Power: the side button. A short press locks the phone. WDA's own lock
# route holds it for half a second, which iOS 27 ignores.
LOCK_PAGE = 0x0C
LOCK_USAGE = 0x30
LOCK_SECONDS = 0.05
LOCK_WAIT = 5.0
LOCK_POLL_INTERVAL = 0.3


def proxy_connect_request_timeout(timeout: float) -> float:
    """Validate WDA's connect budget and leave time for its HTTP response."""
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 300 or not math.isfinite(timeout)):
        raise ValueError("proxy timeout must be a finite number greater than 0 and at most 300 seconds")
    return float(timeout) + 5.0


class WDAError(RuntimeError):
    """An HTTP or WebDriverAgent command failure."""

    def __init__(self, method: str, path: str, status: int, code: str, message: str):
        self.method = method
        self.path = path
        self.status = status
        self.code = code
        super().__init__(f"{method} {path}: {code} ({status}): {message}")


class WDAClient:
    def __init__(self, base_url: str, *, timeout: float = 45.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._http: aiohttp.ClientSession | None = None
        self._session_id: str | None = None
        self._session_lock = asyncio.Lock()
        self._unlocked_at: float | None = None
        self._lock_requests = 0
        # Called with the ended session's id each time another client or a runner restart replaces it.
        self.on_session_replaced: Callable[[str], None] | None = None

    async def __aenter__(self) -> WDAClient:
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def open(self) -> None:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )

    async def close(self) -> None:
        # Delete only a session created by this client.
        try:
            if self._session_id is not None:
                session_id, self._session_id = self._session_id, None
                try:
                    await self.call("DELETE", f"/session/{quote(session_id, safe='')}")
                except (WDAError, aiohttp.ClientError, asyncio.TimeoutError):
                    pass
        finally:
            if self._http is not None:
                await self._http.close()
                self._http = None

    async def call(self, method: str, path: str, body: Any = None, *, request_timeout: float | None = None) -> Any:
        """Send one WDA request and return its WebDriver ``value``."""
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("path must start with one slash")
        kwargs: dict[str, Any] = {}
        if request_timeout is not None:
            if (isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float))
                    or not math.isfinite(request_timeout) or request_timeout <= 0):
                raise ValueError("request timeout must be a finite positive number")
            kwargs["timeout"] = aiohttp.ClientTimeout(total=request_timeout)
        await self.open()
        assert self._http is not None
        if method.upper() in {"POST", "PUT", "PATCH"}:
            # WDA rejects an empty POST body, including on stop/disconnect.
            kwargs["json"] = {} if body is None else body
        async with self._http.request(method, self.base_url + path, **kwargs) as response:
            try:
                payload = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError) as exc:
                raise WDAError(method, path, response.status, "invalid_response", "non-JSON WDA response") from exc
            if not isinstance(payload, dict) or "value" not in payload:
                raise WDAError(method, path, response.status, "invalid_response", "missing value envelope")
            value = payload["value"]
            if isinstance(value, dict) and isinstance(value.get("error"), str):
                raise WDAError(method, path, response.status, value["error"], str(value.get("message", "")))
            if response.status >= 400:
                raise WDAError(method, path, response.status, "http_error", str(value))
            return value

    async def session(self) -> str:
        """Create a session without launching or binding an application."""
        async with self._session_lock:
            if self._session_id is None:
                value = await self.call("POST", "/session", {"capabilities": {"alwaysMatch": {}}})
                if not isinstance(value, dict) or not isinstance(value.get("sessionId"), str):
                    raise WDAError("POST", "/session", 200, "invalid_response", "missing sessionId")
                self._session_id = value["sessionId"]
            return self._session_id

    async def _session_call(self, method: str, path: str, body: Any = None) -> Any:
        sid = await self.session()
        try:
            return await self.call(method, f"/session/{quote(sid, safe='')}{path}", body)
        except WDAError as exc:
            if exc.code != "invalid session id":
                raise
        # WDA keeps one session: another client's new session, or a runner restart, ended ours
        # before the request ran. Without a new one every later session request would fail too.
        async with self._session_lock:
            replaced = self._session_id == sid
            if replaced:
                self._session_id = None
        if replaced:  # Requests that found the same session gone report it once.
            _log.warning("WebDriverAgent session %s at %s was ended by another client or a runner restart; "
                         "starting a new one. If another tool or controller is driving this iPhone, stop it: "
                         "each new session ends the other's.", sid, self.base_url)
            if self.on_session_replaced is not None:
                self.on_session_replaced(sid)
        sid = await self.session()
        return await self.call(method, f"/session/{quote(sid, safe='')}{path}", body)

    async def state(self) -> Any:
        return await self.call("GET", "/mobilerun/state?scale=1")

    async def screenshot(self) -> str:
        # The fork defaults to HEIC (quality 3). MCP images and the fallback
        # decoder need a deterministic format, configured on our own session.
        await self._session_call("POST", "/appium/settings", {"settings": {"screenshotQuality": 0}})
        value = await self.call("GET", "/screenshot")
        if not isinstance(value, str):
            raise WDAError("GET", "/screenshot", 200, "invalid_response", "expected base64 string")
        return value

    async def tap(self, x: float, y: float) -> Any:
        return await self.call("POST", "/mobilerun/actions?scale=1", [
            {"type": "pointerDown", "x": x, "y": y},
            {"type": "pause", "duration": 80},
            {"type": "pointerUp", "x": x, "y": y},
        ])

    async def click_element(self, identifier: str, expected_value: Any = None,
                            *, scroll_to_visible: bool = True) -> Any:
        """Tap one exact accessibility ID through XCTest's element route.

        The fork's scroll route resolves the same cached element before the
        native click. Callers with a verified visible element can skip that
        scroll. Ambiguous or changed controls are left untouched.
        """
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("a nonempty accessibility identifier is required")
        if not isinstance(scroll_to_visible, bool):
            raise ValueError("scroll_to_visible must be a boolean")
        matches = await self._session_call(
            "POST", "/elements", {"using": "accessibility id", "value": identifier}
        )
        path = "/elements"
        if not isinstance(matches, list):
            raise WDAError("POST", path, 200, "invalid_response", "expected an element list")
        if len(matches) != 1:
            code = "no_such_element" if not matches else "ambiguous_element"
            raise WDAError("POST", path, 200, code,
                           f"expected one element with accessibility ID {identifier!r}; found {len(matches)}")
        element = matches[0]
        key = "element-6066-11e4-a52e-4f735466cecf"
        if not isinstance(element, dict) or not isinstance(element.get(key), str) or not element[key]:
            raise WDAError("POST", path, 200, "invalid_response", "missing W3C element identifier")
        element_path = "/element/" + quote(element[key], safe="")
        if expected_value is not None:
            value = await self._session_call("GET", element_path + "/attribute/value")
            if value != expected_value:
                raise WDAError("GET", element_path + "/attribute/value", 200,
                               "value_mismatch", f"expected {expected_value!r}, found {value!r}")
        if scroll_to_visible:
            await self._session_call("POST", "/wda" + element_path + "/scroll", {"toVisible": True})
        return await self._session_call("POST", element_path + "/click")

    async def device_event(self, page: int, usage: int, duration: float) -> Any:
        """Press and release one HID usage, such as a key, for ``duration`` seconds."""
        return await self._session_call("POST", "/wda/performIoHidEvent",
                                        {"page": page, "usage": usage, "duration": duration})

    async def locked(self, *, max_age: float = 0.0) -> bool:
        """Whether the phone is locked. An unlocked reading newer than ``max_age`` seconds is reused;
        a locked one never is, so an unlock is seen at once."""
        if self._unlocked_at is not None and time.monotonic() - self._unlocked_at <= max_age:
            return False
        lock_requests = self._lock_requests
        value = await self.call("GET", "/wda/locked")
        if not isinstance(value, bool):
            raise WDAError("GET", "/wda/locked", 200, "invalid_response", "expected a boolean lock state")
        # An answer that was on its way during a lock request describes the phone before it.
        self._unlocked_at = None if value or lock_requests != self._lock_requests else time.monotonic()
        return value

    async def swipe(self, x1: float, y1: float, x2: float, y2: float, duration_ms: int = 300) -> Any:
        if duration_ms <= 0:
            raise ValueError("duration_ms must be positive")
        return await self.call("POST", "/mobilerun/actions?scale=1", [
            {"type": "pointerDown", "x": x1, "y": y1},
            {"type": "pointerMove", "x": x2, "y": y2, "duration": duration_ms},
            {"type": "pointerUp", "x": x2, "y": y2},
        ])

    async def long_press(self, x: float, y: float, duration_ms: int = 1000) -> Any:
        if duration_ms <= 0:
            raise ValueError("duration_ms must be positive")
        return await self.call("POST", "/mobilerun/actions?scale=1", [
            {"type": "pointerDown", "x": x, "y": y},
            {"type": "pause", "duration": duration_ms},
            {"type": "pointerUp", "x": x, "y": y},
        ])

    async def type_text(self, text: str) -> Any:
        return await self._session_call("POST", "/wda/keys", {"value": [text]})

    async def _screen_size(self) -> tuple[float, float]:
        # UIScreen in a UI-test runner can describe a 320x480 compatibility
        # window. The foreground AX root is in the same point space as input.
        state = await self.state()
        tree = state.get("a11y_tree") if isinstance(state, dict) else None
        bounds = tree.get("boundsInScreen") if isinstance(tree, dict) else None
        if not isinstance(state, dict) or state.get("truncated") or not isinstance(bounds, dict) or not {"left", "top", "right", "bottom"} <= bounds.keys():
            raise WDAError("GET", "/mobilerun/state", 200, "geometry_unavailable", "A complete foreground accessibility snapshot is required")
        width = float(bounds["right"]) - float(bounds["left"])
        height = float(bounds["bottom"]) - float(bounds["top"])
        if width <= 0 or height <= 0:
            raise WDAError("GET", "/mobilerun/state", 200, "geometry_unavailable", "Invalid foreground screen bounds")
        return width, height

    async def back(self) -> Any:
        width, height = await self._screen_size()
        return await self.swipe(5, height / 2, width * 0.65, height / 2, 300)

    async def app_switch(self) -> Any:
        width, height = await self._screen_size()
        if not all(math.isfinite(size) and size > 1 for size in (width, height)):
            raise WDAError("GET", "/mobilerun/state", 200, "geometry_unavailable",
                           "App-switch gesture requires finite screen dimensions greater than one point")
        x = math.floor(width / 2)
        return await self._session_call("POST", "/wda/pressAndDragWithVelocity", {
            "fromX": x, "fromY": height - 1, "toX": x, "toY": math.floor(height * 0.45),
            "pressDuration": 0.05, "holdDuration": 0.2, "velocity": 1000,
        })

    @staticmethod
    def _app_switcher_home_target(state: Any) -> dict[str, tuple[float, ...]] | None:
        error = WDAError("GET", "/mobilerun/state", 200, "home_target_unavailable",
                         "App switcher has no unambiguous, unobstructed background target")
        root = state.get("a11y_tree") if isinstance(state, dict) else None
        if not isinstance(root, dict):
            return None
        try:
            nodes = list(walk(root))
        except (TypeError, RecursionError) as exc:
            raise error from exc
        switchers = [(node, ancestors) for node, ancestors in nodes
                     if node.get("resourceId") == "AppSwitcherContentView"]
        if not switchers:
            return None
        phone, package = state.get("phone_state"), "com.apple.springboard"
        if (state.get("truncated") is not False or not isinstance(phone, dict)
                or phone.get("packageName") != package or phone.get("keyboardVisible") is True
                or root.get("packageName") != package or root.get("className") != "XCUIElementTypeApplication"
                or root.get("isEnabled") is not True or len(switchers) != 1):
            raise error
        if any(not isinstance(node.get("children", []), list)
               or any(not isinstance(child, dict) for child in node.get("children", [])) for node, _ in nodes):
            raise error

        def rectangle(node):
            bounds = node.get("boundsInScreen")
            try:
                values = [bounds[key] for key in ("left", "top", "right", "bottom")]
                if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
                    raise ValueError("nonnumeric bounds")
                left, top, right, bottom = map(float, values)
                if not all(map(math.isfinite, (left, top, right, bottom))) or right <= left or bottom <= top:
                    raise ValueError("nonfinite or empty bounds")
                return left, top, right, bottom
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise error from exc

        viewport = rectangle(root)
        switcher, ancestors = switchers[0]
        statuses = [(node, parents) for node, parents in nodes if node.get("className") == "XCUIElementTypeStatusBar"]
        if (switcher.get("packageName") != package or switcher.get("className") != "XCUIElementTypeOther"
                or switcher.get("isEnabled") is not True or len(statuses) != 1):
            raise error
        status, status_ancestors = statuses[0]
        if status.get("packageName") != package or status.get("isEnabled") is not True:
            raise error
        for node in (switcher, status, *ancestors, *status_ancestors):
            rectangle(node)
            if not visible(node, root):
                raise error

        # Fully visible card/header bounds establish the gap. Partial cards
        # are still blockers, so a clipped card cannot conceal the tap point.
        descendants = [(node, rectangle(node)) for node, _ in walk(switcher) if node is not switcher]
        visible_tops = [bounds[1] for node, bounds in descendants if visible(node, root)]
        if not visible_tops:
            raise error
        band_top, band_bottom = rectangle(status)[3], min(visible_tops)
        x, y = (viewport[0] + viewport[2]) / 2, (band_top + band_bottom) / 2
        if band_bottom - band_top < 12 or not math.isfinite(x) or not math.isfinite(y):
            raise error

        def covers(bounds):
            return bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]

        if not covers(rectangle(switcher)) or any(covers(bounds) for _, bounds in descendants):
            raise error
        for node, _ in nodes:
            modal = node.get("className") in _HOME_MODAL_CLASSES
            interactive = node.get("isClickable") is True or node.get("isFocusable") is True or node.get("className") in {
                "XCUIElementTypeButton", "XCUIElementTypeLink", "XCUIElementTypeTextField", "XCUIElementTypeSecureTextField",
                "XCUIElementTypeTextView", "XCUIElementTypeSwitch", "XCUIElementTypeSlider", "XCUIElementTypePickerWheel",
            }
            if modal or interactive:
                bounds = rectangle(node)
                overlaps_root = max(bounds[0], viewport[0]) < min(bounds[2], viewport[2]) and max(bounds[1], viewport[1]) < min(bounds[3], viewport[3])
                if (modal and overlaps_root) or (interactive and covers(bounds)):
                    raise error
        return {"point": (x, y), "viewport": viewport, "gap": (band_top, band_bottom)}

    async def home(self) -> Any:
        async with asyncio.timeout(self.timeout):
            target = self._app_switcher_home_target(await self.state())
            if target is None:
                # The Home button, not WDA's homescreen route: that one only activates SpringBoard,
                # which is already frontmost under Spotlight, Control Center and the App Library.
                return await self._session_call("POST", "/wda/pressButton", {"name": "home"})
            while True:
                await asyncio.sleep(0.1)
                current = self._app_switcher_home_target(await self.state())
                if current is None:
                    raise WDAError("GET", "/mobilerun/state", 200, "home_target_unavailable",
                                   "Foreground changed before app-switcher dismissal")
                if current == target:
                    break
                target = current
            result = await self.tap(*current["point"])
            previous = None
            while True:
                state = await self.state()
                icons = home_icons(state)
                if icons is not None and any(
                    node.get("resourceId") == "AppSwitcherContentView" or node.get("className") in _HOME_MODAL_CLASSES
                    for node, _ in walk(state["a11y_tree"])
                ):
                    icons = None
                if icons is not None and icons == previous:
                    return result
                previous = icons
                await asyncio.sleep(0.1)

    async def clear_text(self, element_id: str | None = None) -> Any:
        if element_id is None:
            active = await self._session_call("GET", "/element/active")
            if not isinstance(active, dict):
                raise WDAError("GET", "/element/active", 200, "invalid_response", "missing active element")
            element_id = active.get("element-6066-11e4-a52e-4f735466cecf") or active.get("ELEMENT")
        if not isinstance(element_id, str) or not element_id:
            raise ValueError("an active or explicit element is required to clear text")
        return await self._session_call("POST", f"/element/{quote(element_id, safe='')}/clear")

    async def key(self, name: str | int, *, meta_state: int = 0) -> Any:
        if isinstance(meta_state, bool) or not isinstance(meta_state, int) or not 0 <= meta_state <= 0xFFFFFFFF:
            raise ValueError("metaState must be an unsigned 32-bit integer")
        if isinstance(name, int):
            if name == 3:
                return await self.global_action("home")
            if name == 4:
                return await self.back()
            if name == 187:
                return await self.app_switch()
            if name in MODIFIER_KEYCODES:
                return None
            shift = bool(meta_state & 0xC1)
            if 29 <= name <= 54:
                uppercase = shift ^ bool(meta_state & 0x100000)
                return await self.type_text(chr(name + (36 if uppercase else 68)))
            if 7 <= name <= 16:
                return await self.type_text(
                    ")!@#$%^&*("[name - 7] if shift else str(name - 7))
            if 144 <= name <= 153:
                return await self.type_text(str(name - 144))
            punctuation = {
                55: ",<", 56: ".>", 68: "`~", 69: "-_", 70: "=+", 71: "[{",
                72: "]}", 73: "\\|", 74: ";:", 75: "'\"", 76: "/?",
            }
            if name in punctuation:
                return await self.type_text(punctuation[name][int(shift)])
            fixed = {17: "*", 18: "#", 61: "\t", 62: " ", 77: "@", 81: "+",
                     154: "/", 155: "*", 156: "-", 157: "+", 158: ".", 162: "(", 163: ")"}
            if name in fixed:
                return await self.type_text(fixed[name])
            if name in (24, 25, 27):
                name = {24: "volumeup", 25: "volumedown", 27: "camera"}[name]
            elif name in (66, 67):
                return await self.type_text("\n" if name == 66 else "\b")
            else:
                raise ValueError(f"unsupported iOS keyboard key_code: {name}")
        return await self._session_call("POST", "/wda/pressButton", {"name": name})

    async def global_action(self, name: str | int) -> Any:
        if name == 1 or name == "back":
            return await self.back()
        if name == 2:
            name = "home"
        if name == 3 or name == "app_switch":
            return await self.app_switch()
        if name == "home":
            return await self.home()
        if name == "lock":
            return await self.lock()
        if name == "unlock":
            # Wakes the screen so Face ID can run; a passcode still needs the owner.
            return await self.call("POST", "/wda/unlock")
        raise ValueError(f"unsupported global action: {name}")

    async def lock(self) -> None:
        """Lock the phone as its side button does, and return once it is locked."""
        try:
            if await self.locked():
                return  # A press now would wake the screen instead.
            await self.device_event(LOCK_PAGE, LOCK_USAGE, LOCK_SECONDS)
            deadline = time.monotonic() + LOCK_WAIT
            while not await self.locked():
                if time.monotonic() >= deadline:
                    raise WDAError("POST", "/wda/performIoHidEvent", 200, "lock_failed",
                                   f"the phone did not lock within {LOCK_WAIT:g} s of the side-button press")
                await asyncio.sleep(LOCK_POLL_INTERVAL)
        finally:
            self._lock_requests += 1
            self._unlocked_at = None  # Input after a lock request always reads the lock again.

    async def launch_app(self, bundle_id: str) -> Any:
        return await self._session_call("POST", "/wda/apps/launch", {"bundleId": bundle_id})

    async def stop_app(self, bundle_id: str) -> Any:
        return await self._session_call("POST", "/wda/apps/terminate", {"bundleId": bundle_id})

    async def deep_link(self, url: str, bundle_id: str | None = None) -> Any:
        body = {"url": url}
        if bundle_id is not None:
            body["bundleId"] = bundle_id
        return await self._session_call("POST", "/url", body)

    async def clipboard_get(self, content_type: str = "plaintext") -> str:
        value = await self.call("POST", "/wda/getPasteboard", {"contentType": content_type})
        if not isinstance(value, str):
            raise WDAError("POST", "/wda/getPasteboard", 200, "invalid_response", "expected base64 string")
        data = base64.b64decode(value, validate=True)
        return data.decode("utf-8") if content_type == "plaintext" else value

    async def clipboard_set(self, content: str, content_type: str = "plaintext") -> Any:
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        return await self.call("POST", "/wda/setPasteboard", {"contentType": content_type, "content": encoded})

    async def clipboard_set_base64(self, encoded: str, content_type: str = "plaintext") -> Any:
        if not isinstance(encoded, str):
            raise TypeError("text_base64 must be a string")
        base64.b64decode(encoded, validate=True)
        return await self.call("POST", "/wda/setPasteboard", {"contentType": content_type, "content": encoded})

    async def location_get(self) -> Any:
        return await self.call("GET", "/wda/simulatedLocation")

    async def location_set(self, latitude: float, longitude: float) -> Any:
        return await self.call("POST", "/wda/simulatedLocation", {"latitude": latitude, "longitude": longitude})

    async def location_reset(self) -> Any:
        return await self.call("DELETE", "/wda/simulatedLocation")

    async def proxy_connect(self, uri: str, timeout: float = 30.0) -> Any:
        request_timeout = proxy_connect_request_timeout(timeout)
        return await self.call("POST", "/mobilerun/socks5/connect", {"uri": uri, "timeout": timeout},
                               request_timeout=request_timeout)

    async def proxy_disconnect(self) -> Any:
        return await self.call("POST", "/mobilerun/socks5/disconnect")

    async def proxy_stats(self) -> Any:
        return await self.call("GET", "/mobilerun/socks5/stats")

    async def dispatch(self, action: str, params: dict[str, Any]) -> Any:
        """Dispatch the controller's named WDA actions with explicit parameters."""
        p = dict(params)
        p.pop("displayId", None)
        if action == "tap":
            return await self.tap(p["x"], p["y"])
        if action == "swipe":
            if "startX" in p:
                duration_ms = round(float(p.get("duration", 0.3)) * 1000)
                return await self.swipe(p["startX"], p["startY"], p["endX"], p["endY"], duration_ms)
            return await self.swipe(p["x1"], p["y1"], p["x2"], p["y2"], p.get("durationMs", p.get("duration_ms", 300)))
        if action == "long_press":
            return await self.long_press(p["x"], p["y"], p.get("durationMs", p.get("duration_ms", 1000)))
        if action == "type_text":
            text = p.get("text")
            if text is None:
                text = base64.b64decode(p["base64_text"], validate=True).decode("utf-8")
            if p.get("clear"):
                await self.clear_text()
            return await self.type_text(text)
        if action == "clear_text":
            return await self.clear_text(p.get("elementId", p.get("element_id")))
        if action == "key":
            name = p.get("key_code", p.get("name", p.get("key")))
            return await self.key(name, meta_state=p.get("metaState", 0))
        if action == "global_action":
            return await self.global_action(p.get("name", p.get("action")))
        if action in {"launch_app", "stop_app"}:
            return await getattr(self, action)(p.get("bundleId", p.get("bundle_id", p.get("package", p.get("packageName")))))
        if action == "deep_link":
            if p.get("action"):
                raise ValueError("iOS deep links do not support Android intent actions")
            return await self.deep_link(p.get("deepLink", p.get("url")),
                                        p.get("package", p.get("bundleId", p.get("bundle_id"))))
        if action == "clipboard_get":
            return await self.clipboard_get(p.get("contentType", "plaintext"))
        if action == "clipboard_set":
            if "text_base64" in p:
                return await self.clipboard_set_base64(p["text_base64"], p.get("contentType", "plaintext"))
            return await self.clipboard_set(p.get("content", p.get("text")), p.get("contentType", "plaintext"))
        if action == "location_set":
            return await self.location_set(p["latitude"], p["longitude"])
        if action == "proxy_connect":
            return await self.proxy_connect(p["uri"], p.get("timeout", 30.0))
        handlers = {
            "state": self.state, "screenshot": self.screenshot,
            "tap": self.tap, "swipe": self.swipe, "long_press": self.long_press,
            "type_text": self.type_text, "clear_text": self.clear_text,
            "key": self.key, "global_action": self.global_action,
            "launch_app": self.launch_app, "stop_app": self.stop_app, "deep_link": self.deep_link,
            "clipboard_get": self.clipboard_get, "clipboard_set": self.clipboard_set,
            "location_get": self.location_get, "location_set": self.location_set,
            "location_reset": self.location_reset,
            "proxy_connect": self.proxy_connect, "proxy_disconnect": self.proxy_disconnect,
            "proxy_stats": self.proxy_stats,
        }
        try:
            handler = handlers[action]
        except KeyError as exc:
            raise ValueError(f"unsupported WDA action: {action}") from exc
        return await handler()
