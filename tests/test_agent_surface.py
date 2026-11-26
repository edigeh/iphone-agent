"""What an agent sees: typed MCP tools, errors that say what to do, screenshots in tap coordinates."""

import asyncio
import base64
import io
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image

from iphone_agent import cli, core
from iphone_agent.core import ConnectionUnavailable, Controller, ControllerHost
from iphone_agent.wda import WDAClient, WDAError


class Recorder:
    def __init__(self):
        self.calls = []

    async def execute(self, action, params=None):
        self.calls.append((action, params))
        return "ok"


async def call(server, name, arguments):
    result = await server.call_tool(name, arguments)
    return result.is_error, result.content[0].text


async def test_tool_list_gives_every_parameter_a_type():
    tools = {tool.name: tool.input_schema for tool in await cli.create_mcp(Recorder()).list_tools()}
    assert set(tools) == {"iphone_capabilities", "iphone_state", "iphone_screenshot", "iphone_tap", "iphone_swipe",
                          "iphone_type", "iphone_press", "iphone_open", "iphone_action", "iphone_disconnect"}
    assert tools["iphone_tap"]["required"] == ["x", "y"]
    assert tools["iphone_swipe"]["required"] == ["x1", "y1", "x2", "y2"]
    assert tools["iphone_press"]["properties"]["button"]["enum"] == [
        "home", "back", "app_switch", "lock", "unlock", "volumeup", "volumedown"]


@pytest.mark.parametrize("tool,arguments,expected", [
    ("iphone_tap", {"x": 10, "y": 20}, ("tap", {"x": 10, "y": 20})),
    ("iphone_tap", {"x": 10, "y": 20, "hold_ms": 800}, ("long_press", {"x": 10, "y": 20, "durationMs": 800})),
    ("iphone_swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4},
     ("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "durationMs": 300})),
    ("iphone_type", {"text": "olá", "clear": True}, ("type_text", {"text": "olá", "clear": True})),
    ("iphone_press", {"button": "home"}, ("global_action", {"name": "home"})),
    ("iphone_press", {"button": "volumeup"}, ("key", {"name": "volumeup"})),
    ("iphone_open", {"bundle_id": "com.apple.Preferences"}, ("launch_app", {"bundleId": "com.apple.Preferences"})),
    ("iphone_open", {"url": "https://example.com"}, ("deep_link", {"url": "https://example.com"})),
    ("iphone_open", {"url": "app://x", "bundle_id": "com.example"},
     ("deep_link", {"url": "app://x", "bundleId": "com.example"})),
    ("iphone_action", {"action": "stop_app", "params": {"bundleId": "com.example"}},
     ("stop_app", {"bundleId": "com.example"})),
])
async def test_typed_tool_runs_the_matching_controller_action(tool, arguments, expected):
    recorder = Recorder()
    failed, _ = await call(cli.create_mcp(recorder), tool, arguments)
    assert not failed and recorder.calls == [expected]


async def test_screenshot_tool_asks_for_point_scale_by_default_and_passes_a_requested_scale():
    class Screen(Recorder):
        async def execute(self, action, params=None):
            await super().execute(action, params)
            return base64.b64encode(b"png").decode()

    screen = Screen()
    server = cli.create_mcp(screen)
    await server.call_tool("iphone_screenshot", {})
    await server.call_tool("iphone_screenshot", {"scale": 3})
    assert screen.calls == [("screenshot", {"scale": 1.0}), ("screenshot", {"scale": 3.0})]


async def test_open_without_a_target_and_an_unknown_button_are_refused_before_the_phone_is_touched():
    recorder = Recorder()
    server = cli.create_mcp(recorder)
    with pytest.raises(ToolError, match="bundle_id, url or both"):
        await server.call_tool("iphone_open", {})
    with pytest.raises(ToolError, match="button"):
        await server.call_tool("iphone_press", {"button": "power"})
    assert recorder.calls == []


async def test_controller_error_reaches_the_agent_with_its_type_and_message():
    class Locked:
        async def execute(self, action, params=None):
            raise core.PhoneLocked("The iPhone is locked. Unlock it, then retry.")

    with pytest.raises(ToolError, match="PhoneLocked: The iPhone is locked. Unlock it, then retry."):
        await cli.create_mcp(Locked()).call_tool("iphone_tap", {"x": 1, "y": 2})


class Journal:
    def __init__(self):
        self.records = []

    def append(self, event, **fields):
        self.records.append((event, fields))

    def close(self):
        pass


class Device:
    udid = "PHONE"

    async def usb_present(self):
        return False

    async def close(self):
        pass


class OfflineWDA(WDAClient):
    """The real client's parameter handling, with the phone's answers supplied by the test."""

    def __init__(self, *, locked=False):
        super().__init__("http://127.0.0.1:1")
        self.requests = []
        self.is_locked = locked

    async def call(self, method, path, body=None, *, request_timeout=None):
        self.requests.append((method, path, body))
        if path == "/wda/locked":
            return self.is_locked
        if path == "/session":
            return {"sessionId": "S"}
        return None


def controller(wda=None):
    return Controller(Device(), wda or OfflineWDA(), Journal())


@pytest.mark.parametrize("action,params,missing,usage", [
    ("tap", {}, "'x'", "tap takes x and y in screen points"),
    ("tap", {"x": 1}, "'y'", "tap takes x and y in screen points"),
    ("swipe", {"x1": 1, "y1": 2}, "'x2'", "swipe takes x1, y1, x2, y2"),
    ("tap_element", {}, "'identifier'", "tap_element takes the accessibility identifier"),
    ("location_set", {"latitude": 1}, "'longitude'", "location_set takes latitude and longitude"),
])
async def test_missing_parameter_is_named_with_what_the_action_takes(action, params, missing, usage):
    subject = controller()
    with pytest.raises(ValueError, match=f"Missing parameter {missing}: {usage}"):
        await subject.execute(action, params)
    sent = [request for request in subject.wda.requests if request[1] != "/wda/locked"]
    assert sent == []


async def test_key_error_that_is_not_a_parameter_the_caller_left_out_is_reported_as_it_is():
    class Broken(OfflineWDA):
        async def dispatch(self, action, params):
            raise KeyError("x" if action == "tap" else 7)

    subject = controller(Broken())
    with pytest.raises(KeyError):
        await subject.execute("tap", {"x": 1, "y": 2})  # The caller did pass x.
    with pytest.raises(KeyError):
        await subject.execute("swipe", {})  # Not a parameter name at all.


async def test_missing_parameter_is_journaled_as_a_failed_request():
    subject = controller()
    with pytest.raises(ValueError):
        await subject.execute("tap", {})
    events = [(event, fields.get("status")) for event, fields in subject.audit.records]
    assert events == [("request", None), ("result", "error")]


async def test_action_over_its_time_limit_says_which_action_and_what_to_try(monkeypatch):
    class Hung(OfflineWDA):
        async def call(self, method, path, body=None, *, request_timeout=None):
            if path.startswith("/mobilerun/state"):
                await asyncio.sleep(3600)
            return await super().call(method, path, body)

    monkeypatch.setattr(core, "ACTION_TIMEOUT", 0.05)
    with pytest.raises(TimeoutError, match=r"state did not finish within 0\.05 s.*Retry the call, or call reconnect"):
        await controller(Hung()).execute("state")


async def test_timeout_raised_by_the_phone_keeps_its_own_message():
    class Failing(OfflineWDA):
        async def call(self, method, path, body=None, *, request_timeout=None):
            if path.startswith("/mobilerun/state"):
                raise TimeoutError("link went quiet")
            return await super().call(method, path, body)

    with pytest.raises(TimeoutError, match="^link went quiet$"):
        await controller(Failing()).execute("state")


async def test_session_ended_by_another_wda_client_is_replaced_and_the_request_sent_once_more():
    from iphone_agent.wda import WDAError

    class OneSession(WDAClient):
        def __init__(self):
            super().__init__("http://127.0.0.1:1")
            self.created, self.sent, self.live = 0, [], None

        async def call(self, method, path, body=None, *, request_timeout=None):
            if path == "/session":
                self.created += 1
                self.live = f"S{self.created}"
                return {"sessionId": self.live}
            if not path.startswith(f"/session/{self.live}/"):
                raise WDAError(method, path, 404, "invalid session id", "Session was deleted")
            self.sent.append(path)
            return None

    wda = OneSession()
    await wda.device_event(0x07, 0x68, 0.02)
    wda.live = "taken-by-another-client"
    await wda.device_event(0x07, 0x68, 0.02)
    await wda.type_text("a")
    assert wda.created == 2
    assert wda.sent == ["/session/S1/wda/performIoHidEvent", "/session/S2/wda/performIoHidEvent", "/session/S2/wda/keys"]


async def test_replaced_session_is_logged_and_reported_once_even_when_two_requests_find_it_gone(caplog):
    from iphone_agent.wda import WDAError

    class OneSession(WDAClient):
        def __init__(self):
            super().__init__("http://127.0.0.1:1")
            self.created, self.live = 0, None

        async def call(self, method, path, body=None, *, request_timeout=None):
            if path == "/session":
                self.created += 1
                self.live = f"S{self.created}"
                return {"sessionId": self.live}
            await asyncio.sleep(0)
            if not path.startswith(f"/session/{self.live}/"):
                raise WDAError(method, path, 404, "invalid session id", "Session was deleted")
            return None

    wda = OneSession()
    reports = []
    wda.on_session_replaced = reports.append
    await wda.type_text("a")
    assert reports == [] and not caplog.records
    wda.live = "taken-by-another-client"
    await asyncio.gather(wda.type_text("b"), wda.type_text("c"))
    assert reports == ["S1"] and wda.created == 2
    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1 and "S1" in warnings[0] and "another" in warnings[0]


async def test_other_wda_errors_on_a_session_request_are_not_retried():
    from iphone_agent.wda import WDAError

    class Stale(WDAClient):
        def __init__(self):
            super().__init__("http://127.0.0.1:1")
            self.attempts = 0

        async def call(self, method, path, body=None, *, request_timeout=None):
            if path == "/session":
                return {"sessionId": "S"}
            self.attempts += 1
            raise WDAError(method, path, 404, "stale element reference", "gone")

    wda = Stale()
    with pytest.raises(WDAError, match="stale element reference"):
        await wda.type_text("a")
    assert wda.attempts == 1


def png(width, height):
    output = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode()


def size(encoded):
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        return image.size, image.format


class Screen(OfflineWDA):
    def __init__(self, pixels, points):
        super().__init__()
        self.pixels, self.points, self.geometry_reads = pixels, points, 0

    async def screenshot(self):
        return png(*self.pixels)

    async def _screen_size(self):
        self.geometry_reads += 1
        return self.points


async def test_scaled_screenshot_has_one_pixel_per_point_so_image_positions_are_tap_positions():
    subject = controller(Screen((1179, 2556), (393.0, 852.0)))
    assert size(await subject.execute("screenshot", {"scale": 1})) == ((393, 852), "PNG")
    assert size(await subject.execute("screenshot", {"scale": 2})) == ((786, 1704), "PNG")


async def test_scale_at_or_above_the_screen_ratio_returns_the_native_image_untouched():
    subject = controller(Screen((1179, 2556), (393.0, 852.0)))
    assert await subject.execute("screenshot", {"scale": 3}) == png(1179, 2556)
    assert await subject.execute("screenshot", {"scale": 5}) == png(1179, 2556)
    # A screen whose ratio is not a whole number (1080 / 375 = 2.88) still accepts 3.
    mini = controller(Screen((1080, 2340), (375.0, 812.0)))
    assert await mini.execute("screenshot", {"scale": 3}) == png(1080, 2340)
    assert size(await mini.execute("screenshot", {"scale": 1})) == ((375, 812), "PNG")


async def test_screenshot_without_scale_is_returned_exactly_as_the_phone_sent_it():
    wda = Screen((1179, 2556), (393.0, 852.0))
    assert await controller(wda).execute("screenshot") == png(1179, 2556)
    assert wda.geometry_reads == 0


async def test_rotated_screen_scales_with_the_same_ratio_without_reading_geometry_again():
    wda = Screen((1179, 2556), (393.0, 852.0))
    subject = controller(wda)
    await subject.execute("screenshot", {"scale": 1})
    wda.pixels = (2556, 1179)
    assert size(await subject.execute("screenshot", {"scale": 1})) == ((852, 393), "PNG")
    assert wda.geometry_reads == 1


@pytest.mark.parametrize("scale", [0.1, 0, -1, True, "2"])
async def test_screenshot_scale_that_is_not_a_usable_number_is_refused(scale):
    with pytest.raises(ValueError, match="at least 0.25"):
        await controller(Screen((1179, 2556), (393.0, 852.0))).execute("screenshot", {"scale": scale})


async def test_call_after_a_failed_recovery_reconnects_first_instead_of_asking_the_caller_to():
    first, second = controller(), controller()
    reconnects = []

    async def reconnect():
        reconnects.append("reconnect")
        return second, []

    async def close():
        pass

    host = ControllerHost(first, reconnect, close, lambda error: False)
    first.connection_status = {"status": "disconnected", "generation": 1, "errorType": "WDATransportError"}
    await host.execute("tap", {"x": 5, "y": 6})
    assert reconnects == ["reconnect"] and host.controller is second
    assert [request[1] for request in second.wda.requests] == ["/wda/locked", "/mobilerun/actions?scale=1"]
    assert [request for request in first.wda.requests if request[1] != "/wda/locked"] == []


async def test_reconnect_that_fails_again_is_that_call_s_error_and_the_action_does_not_run():
    first = controller()

    async def reconnect():
        raise ConnectionUnavailable("still away")

    async def close():
        pass

    host = ControllerHost(first, reconnect, close, lambda error: False)
    first.connection_status = {"status": "disconnected", "generation": 1}
    with pytest.raises(ConnectionUnavailable, match="still away"):
        await host.execute("tap", {"x": 5, "y": 6})
    assert first.wda.requests == []


async def test_failed_reconnect_before_an_action_starts_no_second_recovery_behind_it():
    first = controller()
    attempts = []

    async def reconnect():
        attempts.append("reconnect")
        raise ConnectionError("link still down")

    async def close():
        pass

    host = ControllerHost(first, reconnect, close, lambda error: True)  # Everything counts as recoverable.
    first.connection_status = {"status": "disconnected", "generation": 1}
    with pytest.raises(ConnectionError, match="link still down"):
        await host.execute("tap", {"x": 5, "y": 6})
    await asyncio.sleep(0.05)
    assert attempts == ["reconnect"] and host._recovery_task is None


async def test_sms_watch_never_reconnects_by_itself_because_it_runs_outside_the_host_lock():
    first = controller()
    attempts = []

    async def reconnect():
        attempts.append("reconnect")
        return controller(), []

    async def close():
        pass

    host = ControllerHost(first, reconnect, close, lambda error: False)
    first.connection_status = {"status": "disconnected", "generation": 1}
    with pytest.raises(ConnectionUnavailable, match="call reconnect"):
        await host.execute("sms_watch", {})
    assert attempts == [] and host.controller is first


async def test_input_reuses_a_fresh_unlocked_reading_and_always_asks_again_after_a_locked_one():
    wda = OfflineWDA()
    subject = controller(wda)

    def lock_reads():
        return sum(1 for request in wda.requests if request[1] == "/wda/locked")

    await subject.execute("tap", {"x": 1, "y": 2})
    await subject.execute("tap", {"x": 1, "y": 2})
    assert lock_reads() == 1  # The second tap trusted the reading taken for the first.

    wda._unlocked_at -= core.LOCK_READING_MAX_AGE + 1  # The reading is now too old.
    await subject.execute("tap", {"x": 1, "y": 2})
    assert lock_reads() == 2

    wda.is_locked = True
    wda._unlocked_at = None
    for _ in range(2):
        with pytest.raises(core.PhoneLocked, match="Unlock it"):
            await subject.execute("tap", {"x": 1, "y": 2})
    assert lock_reads() == 4  # A locked reading is never reused, so an unlock is seen on the next call.
    wda.is_locked = False
    await subject.execute("tap", {"x": 1, "y": 2})
    taps = [request for request in wda.requests if request[1].startswith("/mobilerun/actions")]
    assert len(taps) == 4


@pytest.mark.parametrize("text,value", [("0", 0.0), ("90", 90.0), ("1800.5", 1800.5)])
def test_idle_timeout_argument_accepts_zero_and_positive_seconds(text, value):
    assert cli._idle_seconds(text) == value


@pytest.mark.parametrize("text", ["-1", "inf", "nan", "soon", ""])
def test_idle_timeout_argument_refuses_values_that_would_break_every_connect(text):
    with pytest.raises(cli.argparse.ArgumentTypeError, match="0 or more"):
        cli._idle_seconds(text)


def test_usage_text_exists_for_every_input_action():
    assert core.INPUT_ACTIONS - {"clear_text"} <= set(core.USAGE)
    json.dumps(core.USAGE)


class LockablePhone(OfflineWDA):
    """A phone whose side-button press locks it after ``reads_until_locked`` further lock readings."""

    def __init__(self, *, locked=False, reads_until_locked=1, unlock_works=False):
        super().__init__(locked=locked)
        self.reads_until_locked, self.unlock_works, self._pending = reads_until_locked, unlock_works, None

    async def call(self, method, path, body=None, *, request_timeout=None):
        if path == "/wda/locked" and self._pending is not None:
            self._pending -= 1
            if self._pending <= 0:
                self.is_locked, self._pending = True, None
        if path.endswith("/wda/performIoHidEvent") and self.reads_until_locked is not None:
            self._pending = self.reads_until_locked
        if path == "/wda/unlock":
            self.requests.append((method, path, body))
            if not self.unlock_works:
                raise WDAError(method, path, 500, "unknown error", "Timed out while waiting until the screen gets unlocked")
            self.is_locked = False
            return None
        return await super().call(method, path, body, request_timeout=request_timeout)

    def events(self):
        return [request[2] for request in self.requests if request[1].endswith("/wda/performIoHidEvent")]


async def test_lock_presses_the_side_button_briefly_and_returns_once_the_phone_is_locked(monkeypatch):
    import iphone_agent.wda as wda_module
    monkeypatch.setattr(wda_module, "LOCK_POLL_INTERVAL", 0.01)
    wda = LockablePhone(reads_until_locked=3)
    subject = controller(wda)
    await subject.execute("tap", {"x": 1, "y": 2})  # Leaves a fresh unlocked reading behind.
    await subject.execute("global_action", {"name": "lock"})
    # iOS 27 ignores the half-second press of WDA's own lock route; a short press locks.
    assert wda.events() == [{"page": 0x0C, "usage": 0x30, "duration": 0.05}]
    assert not any(request[1] == "/wda/lock" for request in wda.requests)
    assert wda.is_locked
    with pytest.raises(core.PhoneLocked):  # The reading taken before the lock is not trusted after it.
        await subject.execute("tap", {"x": 1, "y": 2})


async def test_lock_on_a_locked_phone_presses_nothing_because_the_press_would_wake_it():
    wda = LockablePhone(locked=True)
    await controller(wda).execute("global_action", {"name": "lock"})
    assert wda.events() == []


async def test_lock_that_does_not_take_effect_is_an_error_and_input_reads_the_lock_again(monkeypatch):
    import iphone_agent.wda as wda_module
    monkeypatch.setattr(wda_module, "LOCK_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(wda_module, "LOCK_WAIT", 0.2)
    wda = LockablePhone(reads_until_locked=None)
    subject = controller(wda)
    with pytest.raises(WDAError, match="did not lock within 0.2 s"):
        await subject.execute("global_action", {"name": "lock"})
    assert wda._unlocked_at is None


async def test_unlock_that_leaves_the_phone_locked_says_only_the_owner_can_unlock_it():
    wda = LockablePhone(locked=True)
    with pytest.raises(core.PhoneLocked, match="screen is awake.*only its owner can unlock it"):
        await controller(wda).execute("global_action", {"name": "unlock"})


async def test_unlock_that_works_returns_normally():
    wda = LockablePhone(locked=True, unlock_works=True)
    await controller(wda).execute("global_action", {"name": "unlock"})
    assert not wda.is_locked


async def test_unlock_whose_route_times_out_just_before_face_id_succeeds_is_not_an_error():
    """Seen on iOS 27.2: WDA gave up waiting, and the phone read unlocked a second later."""
    class FaceIdArrivesLate(LockablePhone):
        async def call(self, method, path, body=None, *, request_timeout=None):
            if path == "/wda/unlock":
                try:
                    return await super().call(method, path, body, request_timeout=request_timeout)
                finally:
                    self.is_locked = False
            return await super().call(method, path, body, request_timeout=request_timeout)

    wda = FaceIdArrivesLate(locked=True)
    assert await controller(wda).execute("global_action", {"name": "unlock"}) is None



async def test_lock_reading_answered_after_a_lock_request_does_not_bring_back_the_unlocked_reading():
    """Keep-awake reads the lock every 10 seconds; one of its reads can be in flight during a lock."""
    wda = LockablePhone(reads_until_locked=1)
    release, asked = asyncio.Event(), asyncio.Event()
    real_call = wda.call

    async def slow_first_read(method, path, body=None, *, request_timeout=None):
        if path == "/wda/locked" and not asked.is_set():
            asked.set()
            await release.wait()
            return False  # The phone's answer from before the lock.
        return await real_call(method, path, body, request_timeout=request_timeout)

    wda.call = slow_first_read
    in_flight = asyncio.create_task(wda.locked())
    await asked.wait()
    await wda.lock()
    release.set()
    assert await in_flight is False
    assert wda._unlocked_at is None
    with pytest.raises(core.PhoneLocked):
        await controller(wda).execute("tap", {"x": 1, "y": 2})


@pytest.mark.parametrize("params", [{"name": "home"}, {"name": "back"}, {"name": "app_switch"},
                                    {"action": 1}, {"action": 2}, {"action": 3}])
async def test_system_gestures_are_refused_on_a_locked_phone_like_other_input(params):
    wda = LockablePhone(locked=True)
    with pytest.raises(core.PhoneLocked, match="Unlock it"):
        await controller(wda).execute("global_action", params)
    assert [request[1] for request in wda.requests] == ["/wda/locked"]


async def test_lock_and_unlock_are_not_refused_on_a_locked_phone():
    wda = LockablePhone(locked=True, unlock_works=True)
    subject = controller(wda)
    await subject.execute("global_action", {"name": "lock"})
    await subject.execute("global_action", {"name": "unlock"})
    assert not wda.is_locked
