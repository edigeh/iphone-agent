"""WDA-free remote AX, CoreDevice and persistent HID control for iOS 27+."""
from __future__ import annotations

import asyncio
import base64
import math
import plistlib
import signal
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass

from pymobiledevice3.remote.core_device.app_service import AppServiceService
from pymobiledevice3.remote.core_device.hid_service import (
    ASCII_TO_HID, HID_BUTTON_STATE_DOWN, HID_BUTTON_STATE_UP,
    KEY_A, KEY_V, KEY_LEFT_GUI, KEY_LEFT_SHIFT, KEY_ENTER, KEY_BACKSPACE,
    KEY_TAB, KEY_ESC, KEY_LEFT, KEY_RIGHT, KEY_UP, KEY_DOWN, IndigoHIDService,
    UniversalHIDServiceService,
)
from pymobiledevice3.remote.core_device.display_service import DisplayService
from pymobiledevice3.remote.core_device.orientation_service import OrientationService, ORIENTATION_FEATURE
from pymobiledevice3.remote.core_device.pasteboard_service import (
    PasteboardService, POLICY_PROMISE_SECONDARY, snapshot_text, text_item,
)
from pymobiledevice3.remote.core_device.screen_capture_service import ScreenCaptureService
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.remotexpc import remoted_handshake_uuid

from .native_ax import AX_SERVICE, NativeAX
from .native_input import NativeInput


class NativeUnsupported(RuntimeError):
    pass


@dataclass(frozen=True)
class TouchGeometry:
    """One AX generation's interface bounds and portrait HID transform."""
    width: float
    height: float
    rotation: int
    generation: int

    def normalize(self, x, y):
        if (not all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in (x, y))
                or not 0 <= x <= self.width - 1 or not 0 <= y <= self.height - 1):
            raise ValueError("touch point lies outside AX screen geometry")
        portrait_width, portrait_height = sorted((self.width, self.height))
        if self.rotation == 1:
            x, y = y, portrait_height - 1 - x
        elif self.rotation == 2:
            x, y = portrait_width - 1 - x, portrait_height - 1 - y
        elif self.rotation == 3:
            x, y = portrait_width - 1 - y, x
        # The recovered normalizer uses unsigned integer division, not rounding.
        return int(x * 65535 / (portrait_width - 1)), int(y * 65535 / (portrait_height - 1))


class NativeClient:
    """One selected device; resources open lazily and remain owned until close()."""
    ACTION_SERVICES = {
        "state": (AX_SERVICE,), "a11y_tree": (AX_SERVICE,), "phone_state": (AX_SERVICE,),
        "screen_geometry": (AX_SERVICE,), "tap_element": (AX_SERVICE, UniversalHIDServiceService.SERVICE_NAME, DisplayService.SERVICE_NAME),
        "tap": (AX_SERVICE, UniversalHIDServiceService.SERVICE_NAME, DisplayService.SERVICE_NAME),
        "swipe": (AX_SERVICE, UniversalHIDServiceService.SERVICE_NAME, DisplayService.SERVICE_NAME),
        "long_press": (AX_SERVICE, UniversalHIDServiceService.SERVICE_NAME, DisplayService.SERVICE_NAME),
        "touch_actions": (AX_SERVICE, UniversalHIDServiceService.SERVICE_NAME, DisplayService.SERVICE_NAME),
        "type_text": (UniversalHIDServiceService.SERVICE_NAME, DisplayService.SERVICE_NAME, PasteboardService.SERVICE_NAME),
        "clear_text": (UniversalHIDServiceService.SERVICE_NAME, DisplayService.SERVICE_NAME),
        "key": (UniversalHIDServiceService.SERVICE_NAME, DisplayService.SERVICE_NAME, IndigoHIDService.SERVICE_NAME),
        "global_action": (AX_SERVICE, UniversalHIDServiceService.SERVICE_NAME, DisplayService.SERVICE_NAME, IndigoHIDService.SERVICE_NAME),
        "screenshot": (ScreenCaptureService.SERVICE_NAME,),
        "launch_app": (AppServiceService.SERVICE_NAME,), "stop_app": (AppServiceService.SERVICE_NAME,),
        "deep_link": (AppServiceService.SERVICE_NAME,), "packages": (AppServiceService.SERVICE_NAME,),
        "orientation": (OrientationService.SERVICE_NAME,), "orientation_set": (OrientationService.SERVICE_NAME,),
        "clipboard_get": (PasteboardService.SERVICE_NAME,), "clipboard_set": (PasteboardService.SERVICE_NAME,),
    }

    def __init__(self, rsd, *, udid: str, width_points=None, height_points=None, input_lease=None, timeout=10.0):
        if not udid:
            raise ValueError("an expected device UDID is required")
        self.rsd, self.udid, self.timeout = rsd, udid, timeout
        self.width_points, self.height_points = width_points, height_points
        self._geometry = None
        self._owns_connection, self._open, self._rsd_closed = False, False, False
        self._lifecycle_lock = asyncio.Lock()
        self.ax = NativeAX(rsd, timeout=timeout)
        self.input = NativeInput(rsd, input_lease)
        self._clipboard_lock = asyncio.Lock()
        self._launched = {}

    @classmethod
    def from_endpoint(cls, host, port, *, udid, width_points=None, height_points=None,
                      remoted=None, input_lease=None, timeout=10.0):
        if not host or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("explicit RSD host and port are required")
        # Apple link-local remoted endpoints share Apple's host identity. Tunnel
        # endpoints can opt out (their host-wide pymobiledevice3 identity differs).
        if remoted is None:
            remoted = sys.platform == "darwin" and host.lower().startswith("fe80:")
        handshake = remoted_handshake_uuid() if remoted else None
        rsd = RemoteServiceDiscoveryService((host, port), handshake_uuid=handshake)
        return cls(rsd, udid=udid, width_points=width_points, height_points=height_points,
                   input_lease=input_lease, timeout=timeout)

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def open(self):
        async with self._lifecycle_lock:
            if self._open:
                return
            try:
                if self.rsd.peer_info is None or self._rsd_closed:
                    self._owns_connection = True
                    async with asyncio.timeout(self.timeout):
                        await self.rsd.connect()
                    self._rsd_closed = False
                if self.rsd.udid != self.udid:
                    raise RuntimeError(f"RSD device {self.rsd.udid} does not match expected UDID {self.udid}")
                version = self.rsd.product_version
                try:
                    major = int(version.split(".", 1)[0])
                except (ValueError, AttributeError) as exc:
                    raise NativeUnsupported("cannot establish iOS version from RSD") from exc
                if major < 27:
                    raise NativeUnsupported(f"native control requires iOS 27 or newer; device reports {version}")
                self._open = True
            except BaseException:
                if self._owns_connection:
                    self._owns_connection = False
                    self._rsd_closed = True
                    await self.rsd.close()
                raise

    async def close(self):
        async with self._lifecycle_lock:
            self._open = False
            errors = []
            for resource in (self.input, self.ax):
                try:
                    async with asyncio.timeout(self.timeout):
                        await resource.close()
                except BaseException as exc:
                    errors.append(exc)
            if self._owns_connection:
                self._owns_connection = False
                self._rsd_closed = True
                try:
                    await self.rsd.close()
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                raise BaseExceptionGroup("Native backend cleanup failed", errors)

    def service_availability(self):
        services = (self.rsd.peer_info or {}).get("Services", {})
        return {service: service in services for required in self.ACTION_SERVICES.values() for service in required}

    def capability_report(self):
        available = self.service_availability()
        implemented, unavailable = [], {}
        for action, required in self.ACTION_SERVICES.items():
            missing = [s for s in required if not available[s]]
            if missing:
                unavailable[action] = "RSD service not advertised: " + ", ".join(missing)
            elif UniversalHIDServiceService.SERVICE_NAME in required and not self.input.available:
                unavailable[action] = "Requires an owned media lease with verified targeted stop identifiers"
            else:
                implemented.append(action)
        unavailable.update({"proxy_connect": "No native CoreDevice proxy contract", "proxy_disconnect": "No native CoreDevice proxy contract", "proxy_stats": "No native CoreDevice proxy contract"})
        return {"backend": "native", "implemented": sorted(implemented), "unavailable": unavailable,
                "services": available, "coordinateUnits": "points", "hardwareValidated": False,
                "conditional": {"touch_geometry": "Requires named AX interface orientation and matching root bounds in one fresh generation; numeric AX129 is unverified", "stop_app": "Only a process token returned by this client's launch can be stopped", "type_text": "Unicode paste requires a completely preservable clipboard snapshot"}}

    async def _require(self, *services):
        await self.open()
        advertised = (self.rsd.peer_info or {}).get("Services", {})
        missing = [s for s in services if s not in advertised]
        if missing:
            raise NativeUnsupported("RSD service not advertised: " + ", ".join(missing))

    @asynccontextmanager
    async def _service(self, service_type, timeout=None):
        service = service_type(self.rsd)
        try:
            async with asyncio.timeout(timeout or self.timeout):
                await service.connect()
                yield service
        finally:
            async with asyncio.timeout(self.timeout):
                await service.close()

    async def state(self):
        await self._require(AX_SERVICE)
        return await self.ax.snapshot()

    async def screen_geometry(self):
        self._geometry = None
        self.width_points = self.height_points = None
        return self._geometry_from_state(await self.state())

    def _geometry_from_state(self, state):
        self._geometry = None
        self.width_points = self.height_points = None
        bounds = state["a11y_tree"].get("boundsInScreen")
        if state.get("truncated") or not bounds or bounds["left"] != 0 or bounds["top"] != 0:
            raise NativeUnsupported("complete AX screen bounds at origin are required")
        width, height = bounds["right"], bounds["bottom"]
        orientation = state.get("interfaceOrientation")
        rotations = {"portrait": 0, "portraitUpsideDown": 2, "landscapeLeft": 3, "landscapeRight": 1}
        if not isinstance(orientation, str) or orientation not in rotations:
            raise NativeUnsupported("named AX interface orientation from the geometry snapshot is required")
        self.set_geometry(width, height)
        rotation = rotations[orientation]
        if width == height or (width > height) != bool(rotation % 2):
            self.width_points = self.height_points = None
            raise NativeUnsupported("AX bounds and interface orientation disagree")
        generation = state.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            self.width_points = self.height_points = None
            raise NativeUnsupported("fresh AX geometry generation is required")
        self._geometry = TouchGeometry(width, height, rotation, generation)
        return {"width": width, "height": height}

    async def _screen_size(self):
        geometry = await self.screen_geometry()
        return geometry["width"], geometry["height"]

    def set_geometry(self, width_points, height_points):
        self._geometry = None
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 1 for v in (width_points, height_points)):
            raise ValueError("screen point dimensions must be finite and greater than one")
        self.width_points, self.height_points = width_points, height_points

    def _point(self, x, y):
        if self._geometry is None:
            raise NativeUnsupported("verified AX geometry and interface orientation are required")
        return self._geometry.normalize(x, y)

    async def screenshot(self):
        await self._require(ScreenCaptureService.SERVICE_NAME)
        async with self._service(ScreenCaptureService) as service:
            result = await service.capture_screenshot()
        image = result.get("image")
        if not isinstance(image, bytes) or not image:
            raise RuntimeError("native screenshot returned no image bytes")
        return base64.b64encode(image).decode("ascii")

    async def touch_actions(self, actions):
        await self._require(AX_SERVICE, UniversalHIDServiceService.SERVICE_NAME, DisplayService.SERVICE_NAME)
        await self.screen_geometry()
        return await self.input.actions(actions, self._geometry.normalize)

    async def tap(self, x, y):
        return await self.touch_actions([{"type": "pointerDown", "x": x, "y": y}, {"type": "pause", "duration": 50}, {"type": "pointerUp", "x": x, "y": y}])

    async def long_press(self, x, y, duration_ms=1000):
        if duration_ms <= 0:
            raise ValueError("duration_ms must be positive")
        return await self.touch_actions([{"type": "pointerDown", "x": x, "y": y}, {"type": "pause", "duration": duration_ms}, {"type": "pointerUp", "x": x, "y": y}])

    async def swipe(self, x1, y1, x2, y2, duration_ms=300):
        if duration_ms <= 0:
            raise ValueError("duration_ms must be positive")
        return await self.touch_actions([{"type": "pointerDown", "x": x1, "y": y1}, {"type": "pointerMove", "x": x2, "y": y2, "duration": duration_ms}, {"type": "pointerUp", "x": x2, "y": y2}])

    async def click_element(self, identifier, *, expected_value=None, scroll_to_visible=True):
        state = await self.state()
        if state["truncated"]:
            raise NativeUnsupported("cannot target an incomplete AX tree")
        stack, matches = [state["a11y_tree"]], []
        while stack:
            node = stack.pop()
            if node.get("resourceId") == identifier or node.get("elementId") == identifier:
                matches.append(node)
            stack.extend(node["children"])
        if len(matches) != 1:
            raise ValueError("AX identifier must match exactly one element")
        node = matches[0]
        if expected_value is not None and node.get("text") != expected_value:
            raise ValueError("AX element value changed")
        bounds = node.get("boundsInScreen")
        if not bounds or not node["visible"] or not node["enabled"]:
            raise NativeUnsupported("element is not visibly enabled; AX scrolling contract is unavailable")
        self._geometry_from_state(state)
        geometry = self._geometry
        x, y = (bounds["left"] + bounds["right"]) / 2, (bounds["top"] + bounds["bottom"]) / 2
        await self._require(UniversalHIDServiceService.SERVICE_NAME, DisplayService.SERVICE_NAME)
        return await self.input.actions([{"type": "pointerDown", "x": x, "y": y},
                                         {"type": "pause", "duration": 50},
                                         {"type": "pointerUp", "x": x, "y": y}], geometry.normalize)

    async def _keyboard(self, usages):
        await self._require(UniversalHIDServiceService.SERVICE_NAME, DisplayService.SERVICE_NAME)
        return await self.input.keyboard(usages)

    async def clear_text(self, element_id=None):
        if element_id is not None:
            state = await self.state()
            if state["truncated"]:
                raise NativeUnsupported("cannot target an incomplete AX tree")
            stack, matches = [state["a11y_tree"]], []
            while stack:
                node = stack.pop()
                if node.get("elementId") == element_id or node.get("resourceId") == element_id:
                    matches.append(node)
                stack.extend(node["children"])
            if len(matches) != 1 or not matches[0]["focused"]:
                raise NativeUnsupported("clear_text target must be the uniquely focused AX element")
        await self._keyboard([[KEY_LEFT_GUI, KEY_A], [KEY_BACKSPACE]])

    async def _clipboard_snapshot(self, policy=POLICY_PROMISE_SECONDARY):
        await self._require(PasteboardService.SERVICE_NAME)
        async with self._service(PasteboardService, 5) as service:
            result = await service.get(data_policy=policy)
        if not isinstance(result, dict) or not isinstance(result.get("pasteboard"), dict):
            raise RuntimeError("invalid clipboard snapshot")
        if len(plistlib.dumps(result, fmt=plistlib.FMT_BINARY)) > 8 * 1024 * 1024:
            raise ValueError("clipboard snapshot exceeds 8 MiB")
        return result

    async def clipboard_get(self, content_type="plaintext"):
        if content_type != "plaintext":
            raise NativeUnsupported("native clipboard API exposes plaintext only")
        async with self._clipboard_lock:
            return snapshot_text(await self._clipboard_snapshot()) or ""

    async def _clipboard_set(self, items, metadata=None):
        await self._require(PasteboardService.SERVICE_NAME)
        async with self._service(PasteboardService, 5) as service:
            return await service.set(items, source_metadata=metadata)

    async def clipboard_set(self, text, content_type="plaintext"):
        if content_type != "plaintext":
            raise NativeUnsupported("native clipboard API exposes plaintext only")
        if not isinstance(text, str) or len(text.encode("utf-8")) > 1024 * 1024:
            raise ValueError("clipboard text must be at most 1 MiB UTF-8")
        async with self._clipboard_lock:
            return await self._clipboard_set([text_item(text)])

    @staticmethod
    def _clipboard_identity(snapshot):
        metadata = snapshot.get("pasteboard", {}).get("metadata", {})
        nonce, count = metadata.get("nonce"), metadata.get("changeCount")
        return (nonce, count) if nonce is not None and count is not None else None

    async def type_text(self, text, *, clear=False):
        if not isinstance(text, str) or len(text.encode("utf-8")) > 1024 * 1024:
            raise ValueError("text must be at most 1 MiB UTF-8")
        if all(c in ASCII_TO_HID for c in text):
            keys = [[usage, KEY_LEFT_SHIFT] if shifted else [usage] for usage, shifted in (ASCII_TO_HID[c] for c in text)]
            return await self._keyboard(([[KEY_LEFT_GUI, KEY_A], [KEY_BACKSPACE]] if clear else []) + keys)
        # Ensure the input lease before touching user clipboard contents.
        await self._require(UniversalHIDServiceService.SERVICE_NAME, DisplayService.SERVICE_NAME)
        await self.input.open()
        async with self._clipboard_lock:
            old = await self._clipboard_snapshot()
            items = old["pasteboard"].get("items", [])
            has_promises = any(not isinstance(item.get("data", {}).get(uti, {}).get("data"), bytes)
                               for item in items for uti in item.get("types", []))
            plain_types = {"public.utf8-plain-text", "public.plain-text", "public.text"}
            if has_promises and all(set(item.get("types", [])) <= plain_types for item in items):
                old = await self._clipboard_snapshot({"allResolved": {}})
                items = old["pasteboard"].get("items", [])
            for item in items:
                data = item.get("data", {})
                if any(not isinstance(data.get(uti, {}).get("data"), bytes) for uti in item.get("types", [])):
                    raise NativeUnsupported("Unicode typing cannot preserve promised clipboard representations")
            if self._clipboard_identity(old) is None:
                raise NativeUnsupported("Unicode typing requires clipboard change identity")
            owned = await self._clipboard_set([text_item(text)])
            identity = self._clipboard_identity(owned)
            if identity is None:
                raise RuntimeError("clipboard SET did not return change identity; refusing ambiguous restore")
            try:
                await self._keyboard(([[KEY_LEFT_GUI, KEY_A], [KEY_BACKSPACE]] if clear else []) + [[KEY_LEFT_GUI, KEY_V]])
                await asyncio.sleep(0.15)
            finally:
                current = await self._clipboard_snapshot()
                if self._clipboard_identity(current) == identity:
                    await self._clipboard_set(items, old["pasteboard"].get("sourceMetadata"))

    async def _button(self, name):
        buttons = {"home": (0x0C, 0x40, .05), "lock": (0x0C, 0x30, .5), "volumeup": (0x0C, 0xE9, .05), "volumedown": (0x0C, 0xEA, .05), "siri": (0x0C, 0xCF, 1.0)}
        if name not in buttons:
            raise NativeUnsupported(f"unsupported hardware button {name}")
        await self._require(IndigoHIDService.SERVICE_NAME)
        page, usage, hold = buttons[name]
        async with self._service(IndigoHIDService) as service:
            try:
                await service.send_button(page, usage, HID_BUTTON_STATE_DOWN)
                await asyncio.sleep(hold)
            finally:
                await service.send_button(page, usage, HID_BUTTON_STATE_UP)

    async def key(self, key_code):
        usages = {66: KEY_ENTER, 67: KEY_BACKSPACE, 61: KEY_TAB, 111: KEY_ESC, 21: KEY_LEFT, 22: KEY_RIGHT, 19: KEY_UP, 20: KEY_DOWN}
        if key_code in usages:
            return await self._keyboard([[usages[key_code]]])
        if key_code in (3, 4):
            return await self.global_action("home" if key_code == 3 else "back")
        return await self._button({24: "volumeup", 25: "volumedown"}.get(key_code, key_code))

    async def global_action(self, name):
        name = {1: "back", 2: "home", 3: "app_switch"}.get(name, name)
        if name in {"home", "lock"}:
            return await self._button(name)
        if name not in {"back", "app_switch"}:
            raise NativeUnsupported(f"native global action {name!r} has no verified contract")
        width, height = await self._screen_size()
        geometry = self._geometry
        await self._require(UniversalHIDServiceService.SERVICE_NAME, DisplayService.SERVICE_NAME)
        if name == "back":
            actions = [{"type": "pointerDown", "x": 5, "y": height / 2},
                       {"type": "pointerMove", "x": width * .65, "y": height / 2, "duration": 300},
                       {"type": "pointerUp", "x": width * .65, "y": height / 2}]
        else:
            x, end = width / 2, height * .45
            actions = [{"type": "pointerDown", "x": x, "y": height - 1},
                       {"type": "pointerMove", "x": x, "y": end, "duration": 200},
                       {"type": "pause", "duration": 1000},
                       {"type": "pointerUp", "x": x, "y": end}]
        return await self.input.actions(actions, geometry.normalize)

    async def orientation(self, value=None):
        accepted = {"portrait", "portraitUpsideDown", "landscapeLeft", "landscapeRight"}
        if value is not None and value not in accepted:
            raise ValueError("invalid absolute orientation")
        await self._require(OrientationService.SERVICE_NAME)
        if value is not None:
            self.width_points = self.height_points = None
            self._geometry = None
        payload = {"currentOrientation": {}} if value is None else {"changeOrientation": {"_0": value}}
        async with self._service(OrientationService) as service:
            result = await service.service.send_receive_request({"featureIdentifier": ORIENTATION_FEATURE, "messageType": "OrientationRequest", "payload": payload})
        current = result.get("currentDeviceOrientation")
        selected = current if current in accepted else result.get("currentDeviceNonFlatOrientation")
        if selected not in accepted:
            raise RuntimeError("device returned no upright orientation")
        return {"orientation": selected, "locked": result.get("currentDeviceOrientationLocked")}

    @staticmethod
    def launch_input(bundle_id, url=None):
        if not isinstance(bundle_id, str) or not bundle_id:
            raise ValueError("bundleId is required for native launch/deep link")
        options = {"workingDirectory": None, "arguments": [], "environmentVariables": {},
                   "platformSpecificOptions": plistlib.dumps({}, fmt=plistlib.FMT_BINARY),
                   "standardIOUsesPseudoterminals": True, "startStopped": False,
                   "terminateExisting": False, "user": {"active": True}}
        if url is not None:
            if not isinstance(url, str) or not url:
                raise ValueError("deep link URL must be nonempty")
            options["payloadURL"] = {"relative": url}
        return {"applicationSpecifier": {"bundleIdentifier": {"_0": bundle_id}}, "options": options, "standardIOIdentifiers": {}}

    async def launch_app(self, bundle_id, url=None):
        payload = self.launch_input(bundle_id, url)
        await self._require(AppServiceService.SERVICE_NAME)
        async with self._service(AppServiceService) as service:
            result = await service.invoke("com.apple.coredevice.feature.launchapplication", payload)
        if not isinstance(result, dict) or not isinstance(result.get("processToken"), dict):
            raise RuntimeError("launch reply lacks processToken")
        self._launched[bundle_id] = result["processToken"]
        return result

    async def stop_app(self, bundle_id):
        token = self._launched.get(bundle_id)
        pid = token.get("processIdentifier") if token else None
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise NativeUnsupported("stop_app requires this client's launch process token")
        await self._require(AppServiceService.SERVICE_NAME)
        async with self._service(AppServiceService) as service:
            # Check exact token membership to avoid signaling a reused PID.
            processes = await service.list_processes()
            if token not in processes:
                raise RuntimeError("launched process token is no longer current")
            result = await service.send_signal_to_process(pid, signal.SIGTERM)
        self._launched.pop(bundle_id, None)
        return result

    async def packages(self):
        await self._require(AppServiceService.SERVICE_NAME)
        async with self._service(AppServiceService) as service:
            return await service.list_apps()

    async def dispatch(self, action, params):
        p = dict(params)
        if str(p.pop("displayId", 0)) != "0":
            raise NativeUnsupported("only the primary display is supported")
        bundle = p.get("bundleId", p.get("bundle_id", p.get("package", p.get("packageName"))))
        if action in {"state", "screen_geometry", "screenshot", "packages"}:
            return await getattr(self, action)()
        if action in {"a11y_tree", "phone_state"}:
            return (await self.state())[action]
        if action == "capabilities":
            await self.open()
            return self.capability_report()
        if action == "tap":
            return await self.tap(p["x"], p["y"])
        if action == "long_press":
            return await self.long_press(p["x"], p["y"], p.get("durationMs", p.get("duration_ms", 1000)))
        if action == "swipe":
            if "startX" in p:
                return await self.swipe(p["startX"], p["startY"], p["endX"], p["endY"], round(float(p.get("duration", .3)) * 1000))
            return await self.swipe(p["x1"], p["y1"], p["x2"], p["y2"], p.get("durationMs", p.get("duration_ms", 300)))
        if action == "touch_actions":
            return await self.touch_actions(p["actions"])
        if action == "tap_element":
            return await self.click_element(p["identifier"], expected_value=p.get("expectedValue"), scroll_to_visible=p.get("scrollToVisible", True))
        if action == "clear_text":
            return await self.clear_text(p.get("element_id", p.get("elementId")))
        if action == "type_text":
            text = p.get("text")
            if text is None:
                text = base64.b64decode(p["base64_text"], validate=True).decode("utf-8")
            if not isinstance(text, str) or len(text.encode("utf-8")) > 1024 * 1024:
                raise ValueError("text must be at most 1 MiB UTF-8")
            return await self.type_text(text, clear=bool(p.get("clear")))
        if action == "key":
            return await self.key(p.get("key_code", p.get("name")))
        if action == "global_action":
            return await self.global_action(p.get("name", p.get("action")))
        if action in {"launch_app", "deep_link"}:
            return await self.launch_app(bundle, p.get("url") if action == "deep_link" else None)
        if action == "stop_app":
            return await self.stop_app(bundle)
        if action in {"orientation", "orientation_set"}:
            return await self.orientation(p.get("orientation", p.get("value")))
        if action == "clipboard_get":
            return await self.clipboard_get(p.get("contentType", "plaintext"))
        if action == "clipboard_set":
            text = p.get("text", p.get("content"))
            if "text_base64" in p:
                text = base64.b64decode(p["text_base64"], validate=True).decode("utf-8")
            return await self.clipboard_set(text, p.get("contentType", "plaintext"))
        raise NativeUnsupported(f"native action {action!r} has no verified contract")
