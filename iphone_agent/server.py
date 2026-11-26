"""Authenticated local HTTP, WebSocket and JSON-RPC entry points."""
import hmac
import json
from aiohttp import web
from .core import RPC_ACTIONS, Unsupported


ROUTES = {
    "tap": "tap", "swipe": "swipe", "global": "global_action", "app": "launch_app",
    "app/stop": "stop_app", "app/deep-link": "deep_link", "app/uninstall": "uninstall", "app/reset": "app_reset",
    "keyboard/input": "type_text", "input": "type_text", "keyboard/clear": "clear_text", "clear": "clear_text",
    "keyboard/key": "key", "key": "key", "screenshot": "screenshot", "state": "state",
    "state_full": "state", "a11y_tree_full": "a11y_tree", "a11y_tree": "a11y_tree", "phone_state": "phone_state",
    "packages": "packages", "version": "version", "time": "time", "reset": "reset", "install": "install",
    "files/list": "files_list", "files/download": "files_read", "files/upload": "files_write",
    "files/delete": "files_delete", "files/fetch": "files_fetch", "files/push": "files_push",
    "timezone/get": "timezone_get", "timezone/set": "timezone_set", "location/get": "location_get",
    "location/set": "location_set", "location/reset": "location_reset", "clipboard/get": "clipboard_get",
    "clipboard/set": "clipboard_set", "sockstun/connect": "proxy_connect", "sockstun/disconnect": "proxy_disconnect",
    "sockstun/stats": "proxy_stats", "capabilities": "capabilities", "device": "info",
    "stream/start": "stream_video_start", "stream/stop": "stream_video_stop", "stream/list": "stream_video_list",
    "stream/keyframe": "stream_video_keyframe", "stream/broadcast/start": "stream_broadcast_start",
    "stream/audio/start": "stream_audio_start", "stream/audio/stop": "stream_audio_stop", "stream/audio/list": "stream_audio_list",
    "stream/broadcast/stop": "stream_broadcast_stop", "recording/start": "recording_start",
    "recording/stop": "recording_stop", "recording/status": "recording_status",
    "sms/list": "sms_list", "sms/get": "sms_get", "sms/subscriptions": "sms_subscriptions", "sms/refresh": "sms_refresh",
}
GET_PATHS = {"screenshot", "state", "state_full", "a11y_tree_full", "a11y_tree", "phone_state", "version", "packages", "time", "clipboard/get", "capabilities", "device"}


def normalize_method(method):
    if not isinstance(method, str):
        raise ValueError("method must be a string")
    if method in RPC_ACTIONS:
        return method
    if "." in method:
        raise Unsupported(f"Unknown RPC method: {method}")
    service, _, action = method.rpartition("/")
    action = action[:1].upper() + action[1:]
    normalized = f"{service or 'portal'}.{action}"
    if normalized not in RPC_ACTIONS:
        raise Unsupported(f"Unknown RPC method: {method}")
    return normalized


async def rpc(controller, message):
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request must be an object"}}
    request_id = message.get("id")
    try:
        method = normalize_method(message.get("method"))
        params = message.get("params", {})
        result = await controller.execute(method, params)
        response = {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        code = -32601 if isinstance(exc, Unsupported) else -32602 if isinstance(exc, (ValueError, KeyError, TypeError)) else -32000
        response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": str(exc)}}
    return response if "id" in message else None


def create_app(controller, token):
    if not isinstance(token, str) or len(token) < 24:
        raise ValueError("Use a bearer token of at least 24 characters")

    @web.middleware
    async def access(request, handler):
        if request.path == "/ping":
            return await handler(request)
        authorization = request.headers.get("Authorization", "")
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
            raise web.HTTPUnauthorized(text="Bearer token required")
        # Browser-origin requests use the same bearer contract; cookies never authorize actions.
        if request.method == "POST" and request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="application/json required")
        return await handler(request)

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as exc:
            status = 501 if isinstance(exc, Unsupported) else 400 if isinstance(exc, (ValueError, KeyError, TypeError)) else 502
            return web.json_response({"error": {"type": type(exc).__name__, "message": str(exc)}}, status=status)

    app = web.Application(middlewares=[errors, access], client_max_size=32 * 1024 * 1024)
    async def ping(request):
        return web.json_response({"status": "ok"})
    app.router.add_get("/ping", ping)

    async def action_handler(request):
        name = request.path.removeprefix("/action/").lstrip("/")
        params = dict(request.query) if request.method == "GET" else await request.json()
        result = await controller.execute(ROUTES[name], params)
        return web.json_response({"result": result})

    async def rpc_handler(request):
        result = await rpc(controller, await request.json())
        return web.Response(status=204) if result is None else web.json_response(result)

    async def execute_handler(request):
        message = await request.json()
        if not isinstance(message, dict) or not isinstance(message.get("action"), str):
            raise ValueError("An action string is required")
        return web.json_response({"result": await controller.execute(message["action"], message.get("params", {}))})

    async def ws_handler(request):
        ws = web.WebSocketResponse(max_msg_size=32 * 1024 * 1024, heartbeat=30)
        await ws.prepare(request)
        async for message in ws:
            if message.type == web.WSMsgType.TEXT:
                try:
                    result = await rpc(controller, json.loads(message.data))
                except ValueError:
                    result = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}
                if result is not None:
                    await ws.send_json(result)
        return ws

    for name in ROUTES:
        app.router.add_post("/" + name, action_handler)
        app.router.add_post("/action/" + name, action_handler)
        if name in GET_PATHS:
            app.router.add_get("/" + name, action_handler)
    app.router.add_post("/rpc", rpc_handler)
    app.router.add_post("/execute", execute_handler)
    app.router.add_get("/ws", ws_handler)
    return app
