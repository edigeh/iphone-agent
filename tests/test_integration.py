import asyncio
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession, web
from iphone_agent.audit import AuditLog
from iphone_agent.core import Controller
from iphone_agent.cli import create_mcp
from iphone_agent.server import create_app, rpc
from iphone_agent.wda import WDAClient


@pytest.mark.parametrize("server_timeout,expected", [(30, 65), (120, 155), (300, 335)])
async def test_proxy_outer_budget_includes_server_http_and_identity_time(tmp_path, monkeypatch, server_timeout, expected):
    import iphone_agent.core as core
    budgets = []
    real_timeout = asyncio.timeout

    @asynccontextmanager
    async def observed_timeout(delay):
        budgets.append(delay)
        async with real_timeout(delay):
            yield

    monkeypatch.setattr(core.asyncio, "timeout", observed_timeout)
    audit = AuditLog(tmp_path / "proxy-budget.jsonl")
    device, wda = AsyncMock(), AsyncMock()
    device.udid = "fixture-usb-phone"
    wda.dispatch.return_value = {"state": "connected"}
    controller = Controller(device, wda, audit)
    try:
        assert await controller.execute("sockstun.Connect", {"uri": "socks5h://proxy:1080", "timeout": server_timeout}) == {"state": "connected"}
        await controller.execute("version")
        assert budgets == [expected, 120]
        wda.dispatch.assert_awaited_once_with("proxy_connect", {"uri": "socks5h://proxy:1080", "timeout": server_timeout})
        records = AuditLog.verify(audit.path)
        assert [record["event"] for record in records] == ["request", "result", "request", "result"]
        assert records[1]["status"] == "ok"
    finally:
        await controller.close()


@pytest.mark.parametrize("timeout", [0, 301, float("nan"), float("inf"), True, "300", None])
async def test_invalid_proxy_timeout_has_one_audited_error_and_no_dispatch(tmp_path, timeout):
    audit = AuditLog(tmp_path / "invalid-proxy-budget.jsonl")
    device, wda = AsyncMock(), AsyncMock()
    device.udid = "fixture-usb-phone"
    controller = Controller(device, wda, audit)
    try:
        with pytest.raises(ValueError, match="timeout"):
            await controller.execute("proxy_connect", {"uri": "socks5h://proxy:1080", "timeout": timeout})
        wda.dispatch.assert_not_awaited()
        records = AuditLog.verify(audit.path)
        assert [record["event"] for record in records] == ["request", "result"]
        assert records[1]["status"] == "error" and records[1]["error_type"] == "ValueError"
    finally:
        await controller.close()


@pytest.fixture
def lock_state():
    """What the fixture WDA answers to GET /wda/locked. Those reads are counted here, not in ``seen``."""
    return {"locked": False, "reads": 0}


@pytest.fixture
async def stack(tmp_path, lock_state):
    seen = []
    async def wda_request(request):
        if request.path == "/wda/locked":
            lock_state["reads"] += 1
            return web.json_response({"value": lock_state["locked"]})
        body = await request.json() if request.method == "POST" else None
        seen.append((request.method, request.path_qs, body))
        if request.path == "/session":
            value = {"sessionId": "fixture-session"}
        elif request.path == "/screenshot":
            value = "aW1hZ2U="
        else:
            value = {}
        return web.json_response({"value": value})

    wda_app = web.Application()
    wda_app.router.add_route("*", "/{path:.*}", wda_request)
    runner = web.AppRunner(wda_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    device = AsyncMock()
    device.udid = "fixture-usb-phone"
    device.usb_present.return_value = True
    device.files_write.return_value = None
    controller = Controller(device, WDAClient(f"http://127.0.0.1:{port}"), AuditLog(tmp_path / "audit.jsonl"))
    try:
        yield controller, seen, tmp_path / "audit.jsonl"
    finally:
        await controller.close()
        await runner.cleanup()


async def test_exact_reference_rpc_shapes_reach_wda(stack):
    controller, seen, path = stack
    payloads = [
        ("portal.Tap", {"displayId": 0, "x": 50, "y": 60}),
        ("portal.Swipe", {"displayId": 0, "startX": 10, "startY": 500, "endX": 10, "endY": 100, "duration": .4}),
        ("portal.App", {"displayId": 0, "package": "com.example.fixture"}),
        ("keyboard.Input", {"displayId": 0, "base64_text": base64.b64encode("Olá 🔎".encode()).decode(), "clear": False}),
        ("portal.Global", {"action": 2}),
    ]
    for index, (method, params) in enumerate(payloads):
        response = await rpc(controller, {"jsonrpc": "2.0", "id": index, "method": method, "params": params})
        assert "error" not in response, response
    assert ("POST", "/session/fixture-session/wda/apps/launch", {"bundleId": "com.example.fixture"}) in seen
    assert any(request[2] == {"value": ["Olá 🔎"]} for request in seen)
    assert seen[1][2][1]["duration"] == 400
    assert len(AuditLog.verify(path)) == 10
    assert "Olá" not in path.read_text()


@pytest.mark.parametrize("stop", ["shutdown", "cancel"])
async def test_managed_reconnect_is_audited_and_shutdown_or_cancel_never_restarts(tmp_path, stop):
    from iphone_agent.core import ControllerHost
    from iphone_agent.network import is_recoverable_error
    audit_path = tmp_path / "recovery-audit.jsonl"
    audit = AuditLog(audit_path)
    device, wda = AsyncMock(), AsyncMock()
    device.udid = "fixture-usb-phone"
    controller = Controller(device, wda, audit)
    controller.stop_requested = asyncio.Event()
    entered, cancelled = asyncio.Event(), asyncio.Event()
    starts = []

    async def reconnect():
        starts.append(True)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def close():
        await controller.close(close_audit=False)

    host = ControllerHost(controller, reconnect, close, is_recoverable_error)
    task = asyncio.create_task(host.execute("reconnect"))
    try:
        await entered.wait()
        if stop == "shutdown":
            assert await host.execute("shutdown") == {"status": "stopping"}
            assert host.stop_requested.is_set()
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set() and starts == [True]
        records = AuditLog.verify(audit_path)
        requests = [record for record in records if record["event"] == "request"]
        results = {record["request_id"]: record for record in records if record["event"] == "result"}
        assert set(results) == {record["request_id"] for record in requests}
        assert next(result for result in results.values() if result["status"] == "error")["error_type"] == "CancelledError"
    finally:
        await host.close()


async def test_authenticated_failed_mutation_receipt_precedes_recovery_and_is_not_replayed(stack):
    from iphone_agent.core import ControllerHost
    from iphone_agent.network import WDATransportError, is_recoverable_error
    controller, seen, audit_path = stack
    dispatch = controller.wda.dispatch
    recovered = asyncio.Event()
    latest = [controller]

    async def response_lost(action, params):
        await dispatch(action, params)
        raise WDATransportError("response lost after effect")

    controller.wda.dispatch = response_lost

    async def reconnect():
        records = AuditLog.verify(audit_path)
        assert records[-1]["event"] == "request" and records[-1]["action"] == "reconnect"
        assert any(record.get("status") == "error" for record in records)
        await latest[0].close(close_audit=False)
        fresh = Controller(controller.device, WDAClient(controller.wda.base_url), controller.audit)
        latest[0] = fresh
        recovered.set()
        return fresh, []

    async def close():
        await latest[0].close(close_audit=False)

    host = ControllerHost(controller, reconnect, close, is_recoverable_error)
    runner = web.AppRunner(create_app(host, "fixture-token-with-enough-entropy"))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        headers = {"Authorization": "Bearer fixture-token-with-enough-entropy"}
        async with ClientSession() as client:
            async with client.post(url + "/execute", headers=headers, json={"action": "tap", "params": {"x": 1, "y": 2}}) as response:
                assert response.status == 502
            await asyncio.wait_for(recovered.wait(), timeout=2)
            assert sum(method == "POST" and path == "/mobilerun/actions?scale=1" for method, path, _ in seen) == 1
            async with client.post(url + "/execute", headers=headers, json={"action": "tap", "params": {"x": 3, "y": 4}}) as response:
                assert response.status == 200
        assert sum(method == "POST" and path == "/mobilerun/actions?scale=1" for method, path, _ in seen) == 2
        requests = [r for r in AuditLog.verify(audit_path) if r["event"] == "request"]
        assert [r["action"] for r in requests] == ["tap", "reconnect", "tap"]
    finally:
        await runner.cleanup()
        await host.close()


async def test_authenticated_http_and_websocket_use_same_dispatch(stack):
    controller, seen, path = stack
    token = "fixture-token-with-enough-entropy"
    runner = web.AppRunner(create_app(controller, token))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        async with ClientSession() as client:
            async with client.post(url + "/execute", json={"action": "tap", "params": {"x": 1, "y": 2}}) as response:
                assert response.status == 401
            assert seen == []
            headers = {"Authorization": "Bearer " + token}
            async with client.get(url + "/state?displayId=0", headers=headers) as response:
                assert response.status == 200
            async with client.post(url + "/execute", headers=headers, json={"action": "tap", "params": {"x": 1, "y": 2}}) as response:
                assert response.status == 200
            async with client.ws_connect(url + "/ws", headers=headers) as ws:
                await ws.send_json({"id": 44, "method": "portal/global", "params": {"action": 2}})
                response = await ws.receive_json()
                assert response["id"] == 44 and "result" in response
            async with client.post(url + "/reset", headers=headers, json={}) as response:
                assert response.status == 501
        assert len(AuditLog.verify(path)) == 8
    finally:
        await runner.cleanup()


async def test_audit_tampering_and_exclusive_writer(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    audit.append("request", request_id="r1", action="tap")
    with pytest.raises(BlockingIOError):
        AuditLog(path)
    audit.close()
    assert len(AuditLog.verify(path)) == 1
    path.write_text(path.read_text().replace('"tap"', '"erase"'))
    with pytest.raises(ValueError, match="damaged"):
        AuditLog.verify(path)
    reopened = AuditLog(path)  # The tampered chain is kept aside, not continued.
    try:
        assert reopened.set_aside is not None and "erase" in reopened.set_aside.read_text()
        assert AuditLog.verify(path) == []
    finally:
        reopened.close()


async def test_failed_actions_have_error_receipts(stack):
    controller, seen, path = stack
    response = await rpc(controller, {"id": 1, "method": "portal.Tap", "params": {"x": 4}})
    assert response["error"]["code"] == -32602
    assert seen == []
    records = AuditLog.verify(path)
    assert records[-1]["status"] == "error"


async def test_mcp_screenshot_is_image_content(stack, monkeypatch):
    import io
    from PIL import Image
    controller, seen, path = stack
    native = io.BytesIO()
    Image.new("RGB", (30, 60)).save(native, format="PNG")
    monkeypatch.setattr(controller.wda, "screenshot", AsyncMock(return_value=base64.b64encode(native.getvalue()).decode()))
    monkeypatch.setattr(controller.wda, "_screen_size", AsyncMock(return_value=(10.0, 20.0)))
    server = create_mcp(controller)
    # Invoke through the SDK tool registry, which validates and serializes tool output.
    result = await server.call_tool("iphone_screenshot", {})
    assert not result.is_error
    [image] = result.content
    assert (image.type, image.mime_type) == ("image", "image/png")
    with Image.open(io.BytesIO(base64.b64decode(image.data))) as decoded:
        assert decoded.size == (10, 20)  # One pixel per point by default.


async def test_cleanup_failure_still_releases_wda_device_and_journal(tmp_path):
    wda, device, media = AsyncMock(), AsyncMock(), AsyncMock()
    media.close.side_effect = RuntimeError("fixture source disconnected")
    audit = AuditLog(tmp_path / "cleanup.jsonl")
    controller = Controller(device, wda, audit)
    controller.media = media
    with pytest.raises(ExceptionGroup, match="cleanup"):
        await controller.close()
    wda.close.assert_awaited_once()
    device.close.assert_awaited_once()
    assert audit.fd is None


async def test_capabilities_reflect_attached_optional_services(stack):
    controller, seen, path = stack
    first = await controller.execute("capabilities")
    assert "webrtc_offer" not in first["implemented"]
    assert "sms_get" not in first["implemented"]
    controller.media = AsyncMock()
    controller.messages = AsyncMock()
    second = await controller.execute("capabilities")
    assert {"webrtc_offer", "recording_start", "sms_get"} <= set(second["implemented"])


async def test_capabilities_discover_conditional_proxy_actions_without_probing_them(stack):
    controller, seen, _ = stack
    result = await controller.execute("capabilities")
    actions = {"proxy_connect", "proxy_disconnect", "proxy_stats"}
    assert actions <= result["conditional"].keys()
    assert actions.isdisjoint(result["implemented"])
    assert "proxy" not in result["conditional"]
    assert all(result["conditional"][action] == "Tunnel runner and Network Extension profile"
               for action in actions)
    assert seen == [("GET", "/status", None)]


async def test_capabilities_do_not_advertise_absent_usb_services(stack):
    controller, _, _ = stack
    controller.transport = "paired-network"
    controller.device.usb_present.return_value = False
    controller.messages = AsyncMock()
    result = await controller.execute("capabilities")
    assert not result["usbPresent"]
    assert {"info", "files_write", "packages", "sms_refresh"}.isdisjoint(result["implemented"])
    assert "files_write" in result["conditional"]
    assert "sms_get" in result["implemented"]  # Cached snapshot reads do not require USB.


async def test_capabilities_include_attached_keepawake_property(stack):
    from types import SimpleNamespace
    controller, _, _ = stack
    controller.keep_awake = SimpleNamespace(status={"active": True, "replyConfirmed": True}, close=AsyncMock(),
                                            activity=lambda: None)
    result = await controller.execute("capabilities")
    assert result["keepAwake"] == {"active": True, "replyConfirmed": True}


@pytest.mark.parametrize("blocked", [{"tap", "swipe"}, {"screenshot"}])
async def test_hybrid_capabilities_match_only_native_routed_action_availability(stack, blocked):
    from types import SimpleNamespace
    from iphone_agent.native import NativeUnsupported
    controller, _, _ = stack
    reason = "Requires an owned media lease with verified targeted stop identifiers"
    report = {"backend": "native", "implemented": sorted({"tap", "swipe", "screenshot"} - blocked),
              "unavailable": {**{action: reason for action in blocked}, "tap_element": reason,
                              "long_press": reason, "key": "Native key service not advertised"},
              "hardwareValidated": False}

    async def native_dispatch(action, params):
        if action in report["unavailable"]:
            raise NativeUnsupported(report["unavailable"][action])
        return "native-result"

    controller.native = SimpleNamespace(capability_report=lambda: report,
                                        dispatch=AsyncMock(side_effect=native_dispatch), close=AsyncMock())
    controller.wda.click_element = AsyncMock(return_value="wda-result")
    capabilities = await controller.execute("capabilities")
    assert capabilities["native"] == report and not capabilities["native"]["hardwareValidated"]
    assert blocked.isdisjoint(capabilities["implemented"])
    assert all(capabilities["unavailable"][action] == reason for action in blocked)
    assert {"tap_element", "long_press", "key", "state"} <= set(capabilities["implemented"])
    assert {"tap_element", "long_press", "key"}.isdisjoint(capabilities["unavailable"])
    assert await controller.execute("tap_element", {"identifier": "wda-element"}) == "wda-result"
    controller.wda.click_element.assert_awaited_once()
    controller.native.dispatch.assert_not_awaited()
    report["implemented"] = ["tap", "swipe", "screenshot"]
    for action in blocked:
        del report["unavailable"][action]
    refreshed = await controller.execute("capabilities")
    assert {"tap", "swipe", "screenshot"} <= set(refreshed["implemented"])


async def test_cleanup_releases_keepawake_before_shared_provider(tmp_path):
    events = []
    device, wda, keeper = AsyncMock(), AsyncMock(), AsyncMock()
    device.udid = "test-device"
    keeper.status = {"active": False}
    keeper.close.side_effect = lambda: events.append("assertion")
    device.close.side_effect = lambda: events.append("provider")
    audit = AuditLog(tmp_path / "keepawake.jsonl")
    controller = Controller(device, wda, audit)
    controller.keep_awake = keeper
    await controller.close()
    assert events == ["assertion", "provider"]
    assert AuditLog.verify(audit.path)[-1]["operation"] == "stop"


async def test_cancelled_keepawake_close_still_closes_transport_and_audit(tmp_path):
    device, wda, keeper = AsyncMock(), AsyncMock(), AsyncMock()
    keeper.close.side_effect = asyncio.CancelledError
    audit = AuditLog(tmp_path / "cancelled-keepawake.jsonl")
    controller = Controller(device, wda, audit)
    controller.keep_awake = keeper
    with pytest.raises(asyncio.CancelledError):
        await controller.close()
    wda.close.assert_awaited_once()
    device.close.assert_awaited_once()
    assert audit.fd is None


async def test_shutdown_stops_and_close_stops_the_keeper_with_an_audit_record(tmp_path):
    device, wda, keeper = AsyncMock(), AsyncMock(), AsyncMock()
    device.udid = "fixture-usb-phone"
    keeper.activity = lambda: None
    keeper.status = {"active": False}
    audit = AuditLog(tmp_path / "shutdown.jsonl")
    controller = Controller(device, wda, audit)
    controller.keep_awake = keeper
    controller.stop_requested = asyncio.Event()
    assert await controller.execute("shutdown") == {"status": "stopping"}
    assert controller.stop_requested.is_set()
    await controller.close()
    keeper.close.assert_awaited_once()
    records = AuditLog.verify(audit.path)
    assert [record["event"] for record in records] == ["request", "result", "keep_awake"]
    assert records[-1]["operation"] == "stop" and records[-1]["status"] == {"active": False}


@pytest.mark.parametrize("params", [{"handoff": True}, {"handoff": False}, {"other": True}])
async def test_shutdown_with_parameters_is_audited_without_stopping(tmp_path, params):
    device, wda = AsyncMock(), AsyncMock()
    device.udid = "fixture-usb-phone"
    audit = AuditLog(tmp_path / "invalid-shutdown.jsonl")
    controller = Controller(device, wda, audit)
    controller.stop_requested = asyncio.Event()
    try:
        with pytest.raises(ValueError, match="no parameters"):
            await controller.execute("shutdown", params)
        assert not controller.stop_requested.is_set()
        records = AuditLog.verify(audit.path)
        assert [record["event"] for record in records] == ["request", "result"]
        assert records[-1]["status"] == "error"
    finally:
        await controller.close()


async def test_http_shutdown_requires_bearer_and_journals_dispatch(stack):
    controller, seen, path = stack
    controller.stop_requested = asyncio.Event()
    token = "shutdown-fixture-token-with-entropy"
    runner = web.AppRunner(create_app(controller, token))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/execute"
    try:
        async with ClientSession() as client:
            async with client.post(url, json={"action": "shutdown"}) as response:
                assert response.status == 401
            assert not controller.stop_requested.is_set()
            async with client.post(url, json={"action": "shutdown"},
                                   headers={"Authorization": "Bearer " + token}) as response:
                assert response.status == 200
                assert (await response.json())["result"] == {"status": "stopping"}
    finally:
        await runner.cleanup()
    assert controller.stop_requested.is_set()
    assert seen == []
    records = AuditLog.verify(path)
    assert [r["event"] for r in records] == ["request", "result"]

async def test_sms_watch_does_not_block_phone_actions_and_is_audited(stack):
    controller, seen, path = stack
    started, release = asyncio.Event(), asyncio.Event()

    async def watch(action, params):
        assert action == "sms_watch"
        started.set()
        await release.wait()
        return {"messages": [], "cursor": "opaque"}

    controller.messages = AsyncMock()
    controller.messages.dispatch.side_effect = watch
    pending = asyncio.create_task(controller.execute("sms_watch", {"timeout": 30}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        result = await asyncio.wait_for(controller.execute("version"), 1)
        assert isinstance(result, str)
        release.set()
        assert await pending == {"messages": [], "cursor": "opaque"}
    finally:
        release.set()
        await pending
    records = AuditLog.verify(path)
    assert [record["event"] for record in records] == ["request", "request", "result", "result"]
    assert records[0]["action"] == "sms_watch"


# A locked phone: input is refused with the reason, reading the screen still works.

INPUT_PARAMS = {
    "tap": {"x": 1, "y": 2},
    "swipe": {"x1": 1, "y1": 2, "x2": 3, "y2": 4},
    "long_press": {"x": 1, "y": 2},
    "touch_actions": {"actions": [{"type": "pointerDown"}]},
    "tap_element": {"identifier": "element"},
    "type_text": {"text": "hello"},
    "clear_text": {},
    "key": {"key": "a"},
    "launch_app": {"bundleId": "com.example.app"},
    "deep_link": {"url": "example://path"},
}


def test_every_input_action_is_covered_here():
    from iphone_agent.core import INPUT_ACTIONS
    assert set(INPUT_PARAMS) == INPUT_ACTIONS


def lock_aware_controller(tmp_path, locked):
    device, wda = AsyncMock(), AsyncMock()
    device.udid = "fixture-usb-phone"
    wda.locked.return_value = locked
    wda.dispatch.return_value = wda.click_element.return_value = wda.call.return_value = {"done": True}
    return Controller(device, wda, AuditLog(tmp_path / "locked.jsonl")), wda


def wda_input_calls(wda):
    return wda.dispatch.await_count + wda.click_element.await_count + wda.call.await_count


@pytest.mark.parametrize("action", sorted(INPUT_PARAMS))
async def test_input_on_a_locked_phone_is_refused_before_it_reaches_wda(tmp_path, action):
    from iphone_agent.core import PhoneLocked
    controller, wda = lock_aware_controller(tmp_path, locked=True)
    try:
        with pytest.raises(PhoneLocked, match="locked"):
            await controller.execute(action, INPUT_PARAMS[action])
        assert wda_input_calls(wda) == 0
        records = AuditLog.verify(controller.audit.path)
        assert [record["event"] for record in records] == ["request", "result"]
        assert records[-1]["status"] == "error" and records[-1]["error_type"] == "PhoneLocked"
    finally:
        await controller.close()


@pytest.mark.parametrize("action", sorted(INPUT_PARAMS))
async def test_input_on_an_unlocked_phone_reaches_wda(tmp_path, action):
    controller, wda = lock_aware_controller(tmp_path, locked=False)
    try:
        await controller.execute(action, INPUT_PARAMS[action])
        assert wda_input_calls(wda) == 1
        wda.locked.assert_awaited_once()
        assert AuditLog.verify(controller.audit.path)[-1]["status"] == "ok"
    finally:
        await controller.close()


@pytest.mark.parametrize("action, params", [("screenshot", {}), ("state", {}), ("global_action", {"name": "lock"})])
async def test_reading_the_screen_and_the_lock_button_still_run_while_locked(tmp_path, action, params):
    controller, wda = lock_aware_controller(tmp_path, locked=True)
    try:
        assert await controller.execute(action, params) == {"done": True}
        wda.dispatch.assert_awaited_once_with(action, params)
    finally:
        await controller.close()


@pytest.mark.parametrize("locked", [False, True])
async def test_capabilities_report_the_lock_state(stack, lock_state, locked):
    controller, _, _ = stack
    lock_state["locked"] = locked
    result = await controller.execute("capabilities")
    assert result["locked"] is locked and lock_state["reads"] == 1


async def test_a_locked_phone_refuses_input_over_the_real_wda_client(stack, lock_state):
    from iphone_agent.core import PhoneLocked
    controller, seen, _ = stack
    lock_state["locked"] = True
    with pytest.raises(PhoneLocked):
        await controller.execute("tap", {"x": 1, "y": 2})
    assert seen == [] and lock_state["reads"] == 1
    lock_state["locked"] = False
    await controller.execute("tap", {"x": 1, "y": 2})
    assert any(path == "/mobilerun/actions?scale=1" for _, path, _ in seen)


async def test_every_call_counts_as_agent_activity_for_the_keeper(tmp_path):
    controller, _ = lock_aware_controller(tmp_path, locked=False)
    calls = []
    controller.keep_awake = SimpleNamespace(activity=lambda: calls.append(len(AuditLog.verify(controller.audit.path))),
                                            status={"active": True}, close=AsyncMock())
    try:
        await controller.execute("version")
        await controller.execute("tap", {"x": 1, "y": 2})
        with pytest.raises(ValueError):
            await controller.execute("shutdown", {"unexpected": True})  # A failed call counts too.
        assert calls == [0, 2, 4]  # Each one before its request record.
    finally:
        await controller.close()


async def test_activity_from_execute_keeps_the_real_keeper_nudging(tmp_path, monkeypatch):
    import time
    import iphone_agent.keepawake as keepawake
    offset = [0.0]
    monkeypatch.setattr(keepawake, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0]))
    controller, wda = lock_aware_controller(tmp_path, locked=False)
    keeper = controller.keep_awake = await keepawake.ActivityKeepAwake(wda, idle_limit=10, interval=60).open()
    try:
        offset[0] += 11
        assert keeper.status["idleSeconds"] >= 10 and not keeper.status["active"]
        await controller.execute("version")
        assert keeper.status["idleSeconds"] < 10 and keeper.status["active"]
    finally:
        await controller.close()
