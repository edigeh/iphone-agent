"""Audited JSON-RPC and scrcpy input over WebRTC data channels."""
import asyncio
import json
import math
import struct
import time

SCROLL_POINTS_PER_UNIT = 30
SCROLL_MAX_POINTS = 300


class ChannelController:
    def __init__(self, controller):
        self.controller = controller
        self.tasks = set()

    def attach(self, channel):
        queue = asyncio.Queue(maxsize=256)
        task = asyncio.create_task(self._consume(channel, queue))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

        @channel.on("message")
        def message(data):
            arrived = time.monotonic()
            if not isinstance(data, (str, bytes)) or len(data) > 1024 * 1024 or queue.full():
                channel.close()
                return
            queue.put_nowait((data, arrived))

        @channel.on("close")
        def close():
            task.cancel()

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _consume(self, channel, queue):
        pending, fingers, started, offsets, geometry = [], set(), None, {}, None
        while True:
            data, arrived = await queue.get()
            request_id = None
            try:
                if isinstance(data, str):
                    from .server import rpc
                    message = json.loads(data)
                    result = await rpc(self.controller, message)
                    if result is not None:
                        channel.send(json.dumps(result))
                    continue
                if not data:
                    raise ValueError("Empty control packet")
                kind = data[0]
                if kind == 0:  # Inject keycode: type/action/keycode/repeat/metastate
                    if len(data) != 14:
                        raise ValueError("Invalid keycode packet")
                    action, key, _repeat, meta = struct.unpack("<BIII", data[1:])
                    if action == 0:
                        params = {"key_code": key}
                        if meta:
                            params["metaState"] = meta
                        await self.controller.execute("key", params)
                elif kind == 1:
                    text = self._string(data, 1, byte_order="<")
                    await self.controller.execute("type_text", {"text": text})
                elif kind == 2:
                    if len(data) not in (32, 36):
                        raise ValueError("Invalid touch packet")
                    # DecodeTouch reads LE fields, including the opaque pointer
                    # identity. The optional trailing uint32 is a frame timestamp;
                    # local arrival time remains the gesture clock and age limit.
                    action, pointer, x, y, width, height, pressure, button, buttons = struct.unpack("<BQiiHHHII", data[1:32])
                    if not width or not height or not (0 <= x < width and 0 <= y < height):
                        raise ValueError("Touch coordinates require positive frame dimensions and an in-frame point")
                    if action not in (0, 1, 2):
                        raise ValueError("Unsupported touch event")
                    if action == 0 and not fingers:
                        pending, started, offsets = [], arrived, {}
                        geometry = await self.controller.execute("screen_geometry")
                        if not isinstance(geometry, dict) or any(
                            isinstance(geometry.get(key), bool) or not isinstance(geometry.get(key), (int, float))
                            or not math.isfinite(geometry[key]) or geometry[key] <= 0
                            for key in ("width", "height")
                        ):
                            raise ValueError("Touch geometry must have finite positive dimensions")
                    if started is None or not 0 <= arrived - started <= 30 or time.monotonic() - started > 30:
                        raise ValueError("Touch gesture missing down or exceeded budget")
                    point_x = x * geometry["width"] / width
                    point_y = y * geometry["height"] / height
                    if not math.isfinite(point_x) or not math.isfinite(point_y):
                        raise ValueError("Touch point scaling exceeded finite geometry")
                    event = {"type": {0: "pointerDown", 1: "pointerUp", 2: "pointerMove"}[action],
                             "pointerId": pointer, "x": point_x, "y": point_y}
                    if action == 0:
                        if len(fingers) >= 5 or pointer in fingers:
                            raise ValueError("Invalid touch pointer")
                    elif pointer not in fingers:
                        raise ValueError("Touch pointer has no preceding down")
                    # WDA advances each pointer independently. Quantize absolute
                    # arrival offsets once so pauses/moves share one gesture clock.
                    offset = round((arrived - started) * 1000)
                    elapsed = offset - offsets.get(pointer, 0)
                    if elapsed < 0:
                        raise ValueError("Touch pointer arrival time moved backwards")
                    additions = []
                    if action == 2:
                        event["duration"] = elapsed
                    elif elapsed:
                        additions.append({"type": "pause", "duration": elapsed, "pointerId": pointer})
                    additions.append(event)
                    if len(pending) + len(additions) > 4096:
                        raise ValueError("Touch gesture exceeded the 4096 action budget")
                    if action == 0:
                        fingers.add(pointer)
                    elif action == 1:
                        fingers.remove(pointer)
                    pending.extend(additions)
                    offsets[pointer] = offset
                    if not fingers:
                        actions = pending
                        pending, fingers, started, offsets, geometry = [], set(), None, {}, None
                        await self.controller.execute("touch_actions", {"actions": actions})
                elif kind == 3:
                    if fingers:
                        raise ValueError("Scroll cannot interrupt an unfinished touch gesture")
                    await self._scroll(data, arrived)
                elif kind == 4:
                    if len(data) != 2:
                        raise ValueError("Invalid back packet")
                    if data[1] == 0:
                        await self.controller.execute("global_action", {"action": 1})
                elif kind == 8:
                    if len(data) != 2:
                        raise ValueError("Invalid clipboard packet")
                    text = await self.controller.execute("clipboard_get")
                    payload = text.encode("utf-8")
                    channel.send(b"\x00" + struct.pack(">I", len(payload)) + payload)
                elif kind == 9:
                    if len(data) < 14:
                        raise ValueError("Invalid clipboard set packet")
                    sequence, paste = struct.unpack(">QB", data[1:10])
                    text = self._string(data, 10, byte_order=">")
                    await self.controller.execute("clipboard_set", {"text": text})
                    if paste:
                        await self.controller.execute("type_text", {"text": text})
                    channel.send(b"\x01" + struct.pack(">Q", sequence))
                else:
                    raise ValueError(f"Control packet type {kind} is unavailable; use JSON-RPC actions")
            except Exception as exc:
                pending, fingers, started, offsets, geometry = [], set(), None, {}, None
                if channel.readyState == "open":
                    channel.send(json.dumps({"id": request_id, "error": {"message": str(exc)}}))

    async def _scroll(self, data, arrived):
        # The optional uint32 frame timestamp does not authorize stale input;
        # only our local arrival clock controls the operation's age budget.
        if len(data) not in (21, 25):
            raise ValueError("Invalid scroll packet")
        x, y, width, height, horizontal, vertical, _buttons = struct.unpack("<iiHHhhI", data[1:21])
        if not width or not height or not (0 <= x < width and 0 <= y < height):
            raise ValueError("Scroll coordinates require positive frame dimensions and an in-frame point")
        if not horizontal and not vertical:
            return
        if not 0 <= time.monotonic() - arrived <= 30:
            raise ValueError("Scroll packet expired before dispatch")
        geometry = await self.controller.execute("screen_geometry")
        if not isinstance(geometry, dict) or any(
            isinstance(geometry.get(key), bool) or not isinstance(geometry.get(key), (int, float))
            or not math.isfinite(geometry[key]) or geometry[key] <= 1
            for key in ("width", "height")
        ):
            raise ValueError("Scroll geometry must have finite dimensions greater than one point")
        if not 0 <= time.monotonic() - arrived <= 30:
            raise ValueError("Scroll packet expired during geometry lookup")
        point_x = x * geometry["width"] / width
        point_y = y * geometry["height"] / height
        if not math.isfinite(point_x) or not math.isfinite(point_y):
            raise ValueError("Scroll point scaling exceeded finite geometry")
        end_x = min(geometry["width"] - 1, max(0, point_x + max(-SCROLL_MAX_POINTS, min(SCROLL_MAX_POINTS, horizontal * SCROLL_POINTS_PER_UNIT))))
        end_y = min(geometry["height"] - 1, max(0, point_y + max(-SCROLL_MAX_POINTS, min(SCROLL_MAX_POINTS, vertical * SCROLL_POINTS_PER_UNIT))))
        if (end_x, end_y) == (point_x, point_y):
            return
        await self.controller.execute("touch_actions", {"actions": [
            {"type": "pointerMove", "x": point_x, "y": point_y},
            {"type": "pointerDown", "x": point_x, "y": point_y},
            {"type": "pointerMove", "x": end_x, "y": end_y, "duration": 200},
            {"type": "pause", "duration": 120},
            {"type": "pointerUp", "x": end_x, "y": end_y},
        ]})

    @staticmethod
    def _string(data, offset, *, byte_order):
        if len(data) < offset + 4:
            raise ValueError("Truncated control string")
        length = struct.unpack(f"{byte_order}I", data[offset:offset + 4])[0]
        if length > 1024 * 1024 or len(data) != offset + 4 + length:
            raise ValueError("Invalid control string length")
        return data[offset + 4:].decode("utf-8")
