import asyncio
import base64
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from iphone_agent.wda import WDAClient, WDAError


@pytest.fixture
async def wda_server():
    requests = []

    async def handle(request):
        body = await request.json() if request.method == "POST" else None
        requests.append((request.method, request.path_qs, body))
        if request.path == "/session":
            value = {"sessionId": "test-session", "capabilities": {}}
        elif request.path == "/mobilerun/state":
            value = {"a11y_tree": {"boundsInScreen": {"left": 0, "top": 0, "right": 390, "bottom": 844}},
                     "truncated": False}
        elif request.path == "/session/test-session/element/active":
            value = {"element-6066-11e4-a52e-4f735466cecf": "focused-id"}
        elif request.path == "/wda/screen":
            value = {"screenSize": {"width": 390, "height": 844}, "scale": 3}
        elif request.path == "/wda/getPasteboard":
            value = base64.b64encode(b"hello").decode()
        elif request.path == "/screenshot":
            value = "aW1hZ2U="
        else:
            value = None
        return web.json_response({"value": value})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", requests
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_state_and_gestures_use_same_point_scale(wda_server):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        await wda.state()
        await wda.dispatch("tap", {"displayId": 0, "x": 30, "y": 40})
        await wda.dispatch("swipe", {"startX": 20, "startY": 700, "endX": 20,
                                     "endY": 200, "duration": 0.4, "displayId": 0})
    assert requests[:3] == [
        ("GET", "/mobilerun/state?scale=1", None),
        ("POST", "/mobilerun/actions?scale=1", [
            {"type": "pointerDown", "x": 30, "y": 40},
            {"type": "pause", "duration": 80},
            {"type": "pointerUp", "x": 30, "y": 40},
        ]),
        ("POST", "/mobilerun/actions?scale=1", [
            {"type": "pointerDown", "x": 20, "y": 700},
            {"type": "pointerMove", "x": 20, "y": 200, "duration": 400},
            {"type": "pointerUp", "x": 20, "y": 200},
        ]),
    ]


@pytest.mark.asyncio
async def test_proxy_reply_can_outlast_base_http_timeout_without_changing_other_requests():
    requests = []

    async def delayed(request):
        requests.append((request.method, request.path, await request.json() if request.method == "POST" else None))
        await asyncio.sleep(0.1)
        return web.json_response({"value": {"state": "connected"}})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", delayed)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with WDAClient(f"http://127.0.0.1:{port}", timeout=0.03) as wda:
            assert await wda.dispatch("proxy_connect", {"uri": "socks5h://proxy:1080", "timeout": 0.2}) == {"state": "connected"}
            assert wda.timeout == 0.03 and wda._http.timeout.total == 0.03
            with pytest.raises(TimeoutError):
                await wda.call("GET", "/ordinary-request")
        assert requests == [("POST", "/mobilerun/socks5/connect", {"uri": "socks5h://proxy:1080", "timeout": 0.2}),
                            ("GET", "/ordinary-request", None)]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, 300.01, float("inf"), float("-inf"), float("nan"), True, False, "30", None])
async def test_proxy_timeout_rejects_invalid_limits_before_any_request(wda_server, timeout):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        with pytest.raises(ValueError, match="timeout"):
            await wda.proxy_connect("socks5h://proxy:1080", timeout)
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "30"])
async def test_http_request_timeout_override_is_validated_before_request(wda_server, timeout):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        with pytest.raises(ValueError, match="timeout"):
            await wda.call("POST", "/mutation", {}, request_timeout=timeout)
    assert requests == []


@pytest.mark.asyncio
async def test_keyboard_session_and_focused_clear(wda_server):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        await wda.dispatch("type_text", {"displayId": 0, "base64_text": "aGk=", "clear": True})
        await wda.dispatch("key", {"key_code": 67, "displayId": 0})
    assert requests[0] == ("POST", "/session", {"capabilities": {"alwaysMatch": {}}})
    assert requests[1] == ("GET", "/session/test-session/element/active", None)
    assert requests[2] == ("POST", "/session/test-session/element/focused-id/clear", {})
    assert requests[3] == ("POST", "/session/test-session/wda/keys", {"value": ["hi"]})
    assert requests[4] == ("POST", "/session/test-session/wda/keys", {"value": ["\b"]})
    assert requests[-1] == ("DELETE", "/session/test-session", None)


@pytest.mark.asyncio
async def test_screenshot_sets_png_using_real_settings_route(wda_server):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        assert await wda.screenshot() == "aW1hZ2U="
    assert requests[1] == ("POST", "/session/test-session/appium/settings", {"settings": {"screenshotQuality": 0}})
    assert requests[2] == ("GET", "/screenshot", None)


@pytest.mark.asyncio
async def test_app_clipboard_location_and_proxy_routes(wda_server):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        await wda.dispatch("launch_app", {"package": "com.example.app"})
        await wda.dispatch("deep_link", {"deepLink": "example://screen", "package": "com.example.app"})
        await wda.dispatch("clipboard_set", {"text": "hello"})
        await wda.dispatch("clipboard_set", {"text_base64": "AP8=", "contentType": "image"})
        assert await wda.dispatch("clipboard_get", {}) == "hello"
        await wda.dispatch("location_set", {"latitude": 1.2, "longitude": 3.4})
        await wda.dispatch("proxy_connect", {"uri": "socks5h://proxy:1080"})
        await wda.dispatch("proxy_disconnect", {})
    assert ("POST", "/session/test-session/wda/apps/launch", {"bundleId": "com.example.app"}) in requests
    assert ("POST", "/session/test-session/url", {"url": "example://screen", "bundleId": "com.example.app"}) in requests
    assert ("POST", "/wda/setPasteboard", {"contentType": "plaintext", "content": "aGVsbG8="}) in requests
    assert ("POST", "/wda/setPasteboard", {"contentType": "image", "content": "AP8="}) in requests
    assert ("POST", "/wda/simulatedLocation", {"latitude": 1.2, "longitude": 3.4}) in requests
    assert ("POST", "/mobilerun/socks5/connect", {"uri": "socks5h://proxy:1080", "timeout": 30.0}) in requests
    assert ("POST", "/mobilerun/socks5/disconnect", {}) in requests


@pytest.mark.asyncio
@pytest.mark.parametrize("action, params", [("global_action", {"action": 3}), ("key", {"key_code": 187})],
                         ids=["global-action", "keycode"])
async def test_app_switch_uses_press_and_drag(wda_server, action, params):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        await wda.dispatch(action, params)
    assert requests == [
        ("GET", "/mobilerun/state?scale=1", None),
        ("POST", "/session", {"capabilities": {"alwaysMatch": {}}}),
        ("POST", "/session/test-session/wda/pressAndDragWithVelocity", {
            "fromX": 195, "fromY": 843, "toX": 195, "toY": 379,
            "pressDuration": 0.05, "holdDuration": 0.2, "velocity": 1000,
        }),
        ("DELETE", "/session/test-session", None),
    ]


@pytest.mark.parametrize("key,meta,text", [
    (29, 0, "a"), (54, 0, "z"), (41, 1, "M"),
    (29, 0x40, "A"), (29, 0x80, "A"), (29, 0x100000, "A"),
    (29, 0x100001, "a"), (29, 0x1000, "a"),
    (7, 0, "0"), (16, 0, "9"), (7, 1, ")"), (8, 0x40, "!"),
    (16, 0x80, "("), (8, 0x100000, "1"),
    (144, 0, "0"), (153, 1, "9"),
])
async def test_printable_keycodes_reach_text_once(wda_server, key, meta, text):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        await wda.dispatch("key", {"key_code": key, "metaState": meta})
    assert [body for method, path, body in requests if path.endswith("/wda/keys")] == [{"value": [text]}]
    assert not any(path.endswith("/wda/pressButton") for _, path, _ in requests)


@pytest.mark.parametrize("key,plain,shifted", [
    (55, ",", "<"), (56, ".", ">"), (68, "`", "~"), (69, "-", "_"),
    (70, "=", "+"), (71, "[", "{"), (72, "]", "}"), (73, "\\", "|"),
    (74, ";", ":"), (75, "'", '"'), (76, "/", "?"),
])
async def test_punctuation_key_map_with_shift_and_caps(wda_server, key, plain, shifted):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        await wda.dispatch("key", {"key_code": key})
        await wda.dispatch("key", {"key_code": key, "metaState": 0x80})
        await wda.dispatch("key", {"key_code": key, "metaState": 0x100000})
    assert [body for _, path, body in requests if path.endswith("/wda/keys")] == [
        {"value": [plain]}, {"value": [shifted]}, {"value": [plain]}]


@pytest.mark.parametrize("key,text", [
    (17, "*"), (18, "#"), (61, "\t"), (62, " "), (77, "@"), (81, "+"),
    (154, "/"), (155, "*"), (156, "-"), (157, "+"), (158, "."), (162, "("), (163, ")"),
])
async def test_fixed_key_map_ignores_shift_and_caps(wda_server, key, text):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        await wda.dispatch("key", {"key_code": key})
        await wda.dispatch("key", {"key_code": key, "metaState": 0x1000C1})
    assert [body for _, path, body in requests if path.endswith("/wda/keys")] == [
        {"value": [text]}, {"value": [text]}]


@pytest.mark.parametrize("key", [57, 58, 59, 60, 113, 114, 115, 117, 118])
async def test_modifier_keys_alone_have_no_effect(wda_server, key):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        assert await wda.dispatch("key", {"key_code": key, "metaState": 1}) is None
    assert requests == []


async def test_app_switch_refreshes_geometry_and_uses_integer_coordinates(wda_server):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        wda.state = AsyncMock(side_effect=[
            {"a11y_tree": {"boundsInScreen": {"left": 0, "top": 0, "right": 391, "bottom": 845}}},
            {"a11y_tree": {"boundsInScreen": {"left": 0, "top": 0, "right": 845, "bottom": 391}}},
        ])
        await wda.app_switch()
        await wda.app_switch()
        assert wda.state.await_count == 2
    assert [body for _, path, body in requests if path.endswith("/wda/pressAndDragWithVelocity")] == [
        {"fromX": 195, "fromY": 844, "toX": 195, "toY": 380,
         "pressDuration": 0.05, "holdDuration": 0.2, "velocity": 1000},
        {"fromX": 422, "fromY": 390, "toX": 422, "toY": 175,
         "pressDuration": 0.05, "holdDuration": 0.2, "velocity": 1000},
    ]


@pytest.mark.parametrize("width,height", [
    (0, 844), (1, 844), (390, 1), (float("nan"), 844),
    (390, float("inf")), (float("-inf"), 844),
])
async def test_app_switch_invalid_geometry_cannot_create_session_or_send_input(wda_server, width, height):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        wda.state = AsyncMock(return_value={"a11y_tree": {"boundsInScreen": {
            "left": 0, "top": 0, "right": width, "bottom": height,
        }}})
        with pytest.raises(WDAError, match="geometry_unavailable"):
            await wda.app_switch()
    assert requests == []


async def test_app_switch_lost_response_does_not_replay_or_fall_back(wda_server):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        original_call = wda.call

        async def lose_mutation_response(method, path, body=None):
            result = await original_call(method, path, body)
            if path.endswith("/wda/pressAndDragWithVelocity"):
                raise ConnectionError("response lost after gesture was accepted")
            return result

        wda.call = lose_mutation_response
        with pytest.raises(ConnectionError, match="response lost"):
            await wda.app_switch()
    assert sum(path.endswith("/wda/pressAndDragWithVelocity") for _, path, _ in requests) == 1
    assert not any(path.startswith("/mobilerun/actions") for _, path, _ in requests)


def home_ax_node(kind, bounds, *, identifier="", children=None, clickable=False):
    left, top, right, bottom = bounds
    return {"className": kind, "resourceId": identifier, "packageName": "com.apple.springboard",
            "isEnabled": True, "isClickable": clickable,
            "boundsInScreen": {"left": left, "top": top, "right": right, "bottom": bottom},
            "children": [] if children is None else children}


@pytest.fixture
def home_switcher_state():
    viewport = (0, 0, 393, 852)
    full_card = home_ax_node("XCUIElementTypeOther", (86, 124, 357, 711), children=[
        home_ax_node("XCUIElementTypeOther", (86, 83, 358, 125))])
    partial_card = home_ax_node("XCUIElementTypeOther", (292, 119, 567, 715), children=[
        home_ax_node("XCUIElementTypeOther", (292, 78, 567, 120))])
    offscreen_placeholder = home_ax_node("XCUIElementTypeOther", (-100, -100, -90, -90))
    switcher = home_ax_node("XCUIElementTypeOther", viewport, identifier="AppSwitcherContentView",
                            children=[full_card, partial_card, offscreen_placeholder])
    root = home_ax_node("XCUIElementTypeApplication", viewport, children=[
        home_ax_node("XCUIElementTypeWindow", viewport, children=[switcher]),
        home_ax_node("XCUIElementTypeWindow", viewport, children=[
            home_ax_node("XCUIElementTypeStatusBar", (0, 0, 393, 54))]),
    ])
    return {"a11y_tree": root, "phone_state": {"packageName": "com.apple.springboard"}, "truncated": False}


def home_icons_state():
    return {
        "truncated": False, "phone_state": {"packageName": "com.apple.springboard"},
        "a11y_tree": home_ax_node("XCUIElementTypeApplication", (0, 0, 393, 852), children=[
            home_ax_node("XCUIElementTypeIcon", (20, 100, 80, 170), identifier="app.example.one"),
            home_ax_node("XCUIElementTypeIcon", (100, 100, 160, 170), identifier="app.example.two"),
        ]),
    }


@pytest.mark.parametrize("action,params", [
    ("global_action", {"name": "home"}), ("global_action", {"action": 2}), ("key", {"key_code": 3}),
])
async def test_home_dismisses_only_stable_switcher_background_and_verifies_icons(wda_server, home_switcher_state, action, params):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        wda.state = AsyncMock(side_effect=[home_switcher_state, deepcopy(home_switcher_state),
                                          home_icons_state(), home_icons_state()])
        await wda.dispatch(action, params)
        assert wda.state.await_count == 4
    assert requests == [("POST", "/mobilerun/actions?scale=1", [
        {"type": "pointerDown", "x": 196.5, "y": 68.5},
        {"type": "pause", "duration": 80},
        {"type": "pointerUp", "x": 196.5, "y": 68.5},
    ])]


async def test_home_waits_for_stable_target_and_two_matching_icon_snapshots(wda_server, home_switcher_state):
    url, requests = wda_server
    moved = deepcopy(home_switcher_state)
    moved["a11y_tree"]["children"][0]["children"][0]["children"][0]["children"][0]["boundsInScreen"]["top"] = 85
    changed_home = home_icons_state()
    changed_home["a11y_tree"]["children"][1]["resourceId"] = "app.example.three"
    async with WDAClient(url) as wda:
        wda.state = AsyncMock(side_effect=[home_switcher_state, moved, deepcopy(moved),
                                          home_icons_state(), changed_home, deepcopy(changed_home)])
        original_tap = wda.tap

        async def guarded_tap(x, y):
            assert wda.state.await_count == 3
            return await original_tap(x, y)

        wda.tap = guarded_tap
        await wda.global_action("home")
        assert wda.state.await_count == 6
    assert len(requests) == 1
    assert requests[0][2][0] == {"type": "pointerDown", "x": 196.5, "y": 69.5}


@pytest.mark.parametrize("invalid", [
    "truncated", "missing-completeness", "wrong-foreground", "wrong-root-package", "wrong-root-kind",
    "duplicate-switcher", "disabled-switcher", "offscreen-switcher", "missing-status", "duplicate-status",
    "offscreen-status", "nonfinite-status", "nonfinite-card", "missing-card-bounds", "boolean-card-bound",
    "empty-card", "malformed-children", "short-gap", "offscreen-only", "partial-card-cover",
    "alert", "sheet", "keyboard", "interactive-leaf", "button-without-clickable-flag",
])
async def test_home_unsafe_switcher_state_cannot_tap_or_fall_back(wda_server, home_switcher_state, invalid):
    url, requests = wda_server
    state = deepcopy(home_switcher_state)
    root = state["a11y_tree"]
    switcher = root["children"][0]["children"][0]
    status = root["children"][1]["children"][0]
    card = switcher["children"][0]
    if invalid == "truncated":
        state["truncated"] = True
    elif invalid == "missing-completeness":
        del state["truncated"]
    elif invalid == "wrong-foreground":
        state["phone_state"]["packageName"] = "app.example.foreground"
    elif invalid == "wrong-root-package":
        root["packageName"] = "app.example.foreground"
    elif invalid == "wrong-root-kind":
        root["className"] = "XCUIElementTypeOther"
    elif invalid == "duplicate-switcher":
        root["children"][0]["children"].append(deepcopy(switcher))
    elif invalid == "disabled-switcher":
        switcher["isEnabled"] = False
    elif invalid == "offscreen-switcher":
        switcher["boundsInScreen"]["right"] = 400
    elif invalid == "missing-status":
        root["children"].pop()
    elif invalid == "duplicate-status":
        root["children"][1]["children"].append(deepcopy(status))
    elif invalid == "offscreen-status":
        status["boundsInScreen"]["left"] = -1
    elif invalid == "nonfinite-status":
        status["boundsInScreen"]["bottom"] = float("inf")
    elif invalid == "nonfinite-card":
        card["boundsInScreen"]["left"] = float("nan")
    elif invalid == "missing-card-bounds":
        del card["boundsInScreen"]
    elif invalid == "boolean-card-bound":
        card["boundsInScreen"]["left"] = True
    elif invalid == "empty-card":
        card["boundsInScreen"]["right"] = 86
    elif invalid == "malformed-children":
        card["children"].append(None)
    elif invalid == "short-gap":
        card["children"][0]["boundsInScreen"]["top"] = 60
    elif invalid == "offscreen-only":
        switcher["children"].pop(0)
    elif invalid == "partial-card-cover":
        switcher["children"].append(home_ax_node("XCUIElementTypeOther", (-10, 60, 250, 100)))
    elif invalid in {"alert", "sheet", "keyboard"}:
        kind = {"alert": "Alert", "sheet": "Sheet", "keyboard": "Keyboard"}[invalid]
        root["children"].append(home_ax_node(f"XCUIElementType{kind}", (0, 600, 393, 852)))
    else:
        root["children"].append(home_ax_node("XCUIElementTypeButton", (180, 55, 215, 82),
                                             clickable=invalid == "interactive-leaf"))
    async with WDAClient(url) as wda:
        wda.state = AsyncMock(return_value=state)
        with pytest.raises(WDAError, match="home_target_unavailable"):
            await wda.global_action("home")
    assert requests == []


@pytest.mark.parametrize("change", ["foreground", "incomplete", "modal"])
async def test_home_switcher_guard_is_rechecked_immediately_before_tap(wda_server, home_switcher_state, change):
    url, requests = wda_server
    changed = deepcopy(home_switcher_state)
    if change == "foreground":
        changed = {"truncated": False, "phone_state": {"packageName": "app.example.foreground"},
                   "a11y_tree": {"packageName": "app.example.foreground", "children": []}}
    elif change == "incomplete":
        changed["truncated"] = True
    else:
        changed["a11y_tree"]["children"].append(home_ax_node("XCUIElementTypeAlert", (100, 60, 300, 400)))
    async with WDAClient(url) as wda:
        wda.state = AsyncMock(side_effect=[home_switcher_state, changed])
        with pytest.raises(WDAError, match="home_target_unavailable"):
            await wda.global_action("home")
    assert requests == []


@pytest.mark.parametrize("state", [None, {}, {"a11y_tree": {"boundsInScreen": {"left": 0, "top": 0, "right": 390, "bottom": 844}}}])
async def test_home_outside_the_switcher_presses_the_home_button(wda_server, state):
    """WDA's homescreen route only activates SpringBoard, which leaves Spotlight, Control Center
    and the App Library open; the button leaves all of them."""
    url, requests = wda_server
    async with WDAClient(url) as wda:
        wda.state = AsyncMock(return_value=state)
        await wda.global_action("home")
        wda.state.assert_awaited_once()
    assert [request for request in requests if "/wda/" in request[1]] == [
        ("POST", "/session/test-session/wda/pressButton", {"name": "home"})]


async def test_home_switcher_lost_tap_response_never_replays_or_falls_back(wda_server, home_switcher_state):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        wda.state = AsyncMock(return_value=home_switcher_state)
        original_call = wda.call

        async def lose_tap_response(method, path, body=None):
            result = await original_call(method, path, body)
            if path.startswith("/mobilerun/actions"):
                raise ConnectionError("tap response lost after acceptance")
            return result

        wda.call = lose_tap_response
        with pytest.raises(ConnectionError, match="tap response lost"):
            await wda.global_action("home")
        assert wda.state.await_count == 2
    assert len(requests) == 1 and requests[0][1] == "/mobilerun/actions?scale=1"


async def test_home_switcher_verification_is_bounded_without_replaying_tap(wda_server, home_switcher_state):
    url, requests = wda_server
    async with WDAClient(url, timeout=0.35) as wda:
        wda.state = AsyncMock(return_value=home_switcher_state)
        with pytest.raises(TimeoutError):
            await wda.global_action("home")
    assert len(requests) == 1 and requests[0][1] == "/mobilerun/actions?scale=1"


async def test_home_switcher_icons_behind_switcher_or_modal_cannot_establish_home(wda_server, home_switcher_state):
    url, requests = wda_server
    hidden_icons = home_icons_state()
    hidden_icons["a11y_tree"]["children"].append(home_ax_node("XCUIElementTypeOther", (0, 0, 393, 852),
                                                               identifier="AppSwitcherContentView"))
    modal_icons = home_icons_state()
    modal_icons["a11y_tree"]["children"].append(home_ax_node("XCUIElementTypeAlert", (100, 100, 300, 400)))
    async with WDAClient(url) as wda:
        wda.state = AsyncMock(side_effect=[home_switcher_state, deepcopy(home_switcher_state),
                                          hidden_icons, deepcopy(hidden_icons), modal_icons, deepcopy(modal_icons),
                                          home_icons_state(), home_icons_state()])
        await wda.global_action("home")
        assert wda.state.await_count == 8
    assert len(requests) == 1


@pytest.mark.parametrize("meta", [True, -1, 2**32, 1.0, "1", None])
async def test_invalid_key_modifier_state_cannot_send_input(wda_server, meta):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        with pytest.raises(ValueError, match="metaState"):
            await wda.dispatch("key", {"key_code": 29, "metaState": meta})
    assert requests == []


@pytest.mark.asyncio
async def test_wda_error_envelope_is_not_reported_as_success():
    async def fail(_request):
        return web.json_response({"value": {"error": "invalid argument", "message": "bad coordinate"}}, status=400)

    app = web.Application()
    app.router.add_get("/mobilerun/state", fail)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with WDAClient(f"http://127.0.0.1:{port}") as wda:
            with pytest.raises(WDAError, match="invalid argument"):
                await wda.state()
    finally:
        await runner.cleanup()


@pytest.fixture
async def element_server():
    requests = []
    state = {"matches": [{"element-6066-11e4-a52e-4f735466cecf": "switch-uuid"}],
             "value": "0", "scroll_error": False}

    async def handle(request):
        body = await request.json() if request.method == "POST" else None
        requests.append((request.method, request.path, body))
        if request.path == "/session":
            value = {"sessionId": "test-session", "capabilities": {}}
        elif request.path == "/session/test-session/elements":
            value = state["matches"]
        elif request.path == "/session/test-session/element/switch-uuid/attribute/value":
            value = state["value"]
        elif request.path == "/session/test-session/wda/element/switch-uuid/scroll" and state["scroll_error"]:
            return web.json_response({"value": {"error": "invalid element state", "message": "cannot scroll"}}, status=400)
        else:
            value = None
        return web.json_response({"value": value})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", requests, state
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_click_element_checks_value_then_scrolls_and_clicks_once(element_server):
    url, requests, _state = element_server
    async with WDAClient(url) as wda:
        await wda.click_element("Auto-Lock", expected_value="0")
    assert requests[:5] == [
        ("POST", "/session", {"capabilities": {"alwaysMatch": {}}}),
        ("POST", "/session/test-session/elements", {"using": "accessibility id", "value": "Auto-Lock"}),
        ("GET", "/session/test-session/element/switch-uuid/attribute/value", None),
        ("POST", "/session/test-session/wda/element/switch-uuid/scroll", {"toVisible": True}),
        ("POST", "/session/test-session/element/switch-uuid/click", {}),
    ]
    assert sum(path.endswith("/click") for _, path, _ in requests) == 1


@pytest.mark.asyncio
async def test_click_visible_element_skips_scroll_route_and_keeps_identity_value_guard(element_server):
    url, requests, state = element_server
    state["scroll_error"] = True
    async with WDAClient(url) as wda:
        await wda.click_element("com.apple.settings.battery", expected_value="0", scroll_to_visible=False)
    assert not any(path.endswith("/scroll") for _, path, _ in requests)
    assert requests[1:4] == [
        ("POST", "/session/test-session/elements", {"using": "accessibility id", "value": "com.apple.settings.battery"}),
        ("GET", "/session/test-session/element/switch-uuid/attribute/value", None),
        ("POST", "/session/test-session/element/switch-uuid/click", {}),
    ]
    assert sum(path.endswith("/click") for _, path, _ in requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 0, 1, "false"])
async def test_click_element_rejects_nonboolean_scroll_option_before_requests(element_server, value):
    url, requests, _state = element_server
    async with WDAClient(url) as wda:
        with pytest.raises(ValueError, match="must be a boolean"):
            await wda.click_element("com.apple.settings.battery", scroll_to_visible=value)
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("matches", [[],
    [{"element-6066-11e4-a52e-4f735466cecf": "a"},
     {"element-6066-11e4-a52e-4f735466cecf": "b"}]])
async def test_click_element_rejects_missing_or_ambiguous_matches(element_server, matches):
    url, requests, state = element_server
    state["matches"] = matches
    async with WDAClient(url) as wda:
        with pytest.raises(WDAError, match="expected one element"):
            await wda.click_element("Auto-Lock")
    assert not any(path.endswith(("/scroll", "/click")) for _, path, _ in requests)


@pytest.mark.asyncio
async def test_click_element_rejects_bad_value_and_scroll_failure_without_click(element_server):
    url, requests, state = element_server
    async with WDAClient(url) as wda:
        with pytest.raises(WDAError, match="value_mismatch"):
            await wda.click_element("Auto-Lock", expected_value="1")
        assert not any(path.endswith(("/scroll", "/click")) for _, path, _ in requests)
        state["scroll_error"] = True
        with pytest.raises(WDAError, match="invalid element state"):
            await wda.click_element("Auto-Lock", expected_value="0")
    assert not any(path.endswith("/click") for _, path, _ in requests)


@pytest.mark.asyncio
async def test_device_event_sends_one_hid_event_in_the_session(wda_server):
    url, requests = wda_server
    async with WDAClient(url) as wda:
        await wda.device_event(0x07, 0x68, 0.02)
    events = [request for request in requests if request[1].endswith("/performIoHidEvent")]
    assert events == [("POST", "/session/test-session/wda/performIoHidEvent",
                       {"page": 0x07, "usage": 0x68, "duration": 0.02})]


@pytest.fixture
async def lock_server():
    """A WDA whose GET /wda/locked answers with ``reply["value"]``."""
    requests, reply = [], {"value": False}

    async def handle(request):
        requests.append((request.method, request.path_qs))
        return web.json_response({"value": reply["value"]})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}", requests, reply
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False])
async def test_locked_reads_the_lock_state_without_a_session(lock_server, value):
    url, requests, reply = lock_server
    reply["value"] = value
    async with WDAClient(url) as wda:
        assert await wda.locked() is value
    assert requests == [("GET", "/wda/locked")]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 0, 1, "false", {}, []])
async def test_locked_rejects_a_reply_that_is_not_a_boolean(lock_server, value):
    url, _, reply = lock_server
    reply["value"] = value
    async with WDAClient(url) as wda:
        with pytest.raises(WDAError, match="boolean"):
            await wda.locked()


@pytest.mark.asyncio
async def test_an_unlocked_reading_is_reused_only_within_max_age(lock_server, monkeypatch):
    import iphone_agent.wda as wda_module
    url, requests, reply = lock_server
    now = [1000.0]
    monkeypatch.setattr(wda_module.time, "monotonic", lambda: now[0])
    async with WDAClient(url) as wda:
        assert await wda.locked(max_age=2.0) is False
        now[0] += 1.5
        assert await wda.locked(max_age=2.0) is False
        assert len(requests) == 1  # Reused.
        assert await wda.locked() is False  # The default reads again.
        assert len(requests) == 2
        now[0] += 2.5
        reply["value"] = True
        assert await wda.locked(max_age=2.0) is True
        assert len(requests) == 3


@pytest.mark.asyncio
async def test_a_locked_reading_is_never_reused(lock_server):
    url, requests, reply = lock_server
    reply["value"] = True
    async with WDAClient(url) as wda:
        assert await wda.locked(max_age=60.0) is True
        reply["value"] = False
        assert await wda.locked(max_age=60.0) is False  # The unlock is seen at once.
    assert len(requests) == 2
