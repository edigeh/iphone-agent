import asyncio
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import iphone_agent.control as control_module
from iphone_agent.control import ChannelController


class Channel:
    def __init__(self):
        self.events, self.sent = {}, []
        self.readyState = "open"

    def on(self, name):
        def register(fn):
            self.events[name] = fn
            return fn
        return register

    def send(self, value):
        self.sent.append(value)

    def close(self):
        self.readyState = "closed"
        self.events["close"]()


async def wait_for(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(.001)


def touch(action, pointer=0, x=300, y=600, width=1170, height=2532):
    return b"\x02" + struct.pack("<BQiiHHHII", action, pointer, x, y, width, height, 65535, 1, 1)


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    # Replace this module's clock reference without changing asyncio's clock.
    monkeypatch.setattr(control_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def send_touch(channel, clock, elapsed, action, pointer=0, **coordinates):
    clock.now = 100.0 + elapsed
    channel.events["message"](touch(action, pointer, **coordinates))


def wda_timeline(actions):
    """Interpret the pinned WDA fork's independent offset for each pointer."""
    offsets, events = {}, []
    for action in actions:
        pointer = action["pointerId"]
        offset = offsets.get(pointer, 0)
        duration = action.get("duration", 0)
        if action["type"] == "pointerMove":
            events.append((pointer, action["type"], offset + duration))
        elif action["type"] != "pause":
            events.append((pointer, action["type"], offset))
        offsets[pointer] = offset + duration
    return events


async def test_staggered_two_finger_pinch_preserves_shared_arrival_timeline(clock):
    controller = AsyncMock()
    controller.execute.return_value = {"width": 390, "height": 844}
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        for elapsed, action, pointer in [(0, 0, 0), (.18, 0, 1), (.3, 2, 0), (.4, 2, 1), (.8, 1, 0), (.9, 1, 1)]:
            send_touch(channel, clock, elapsed, action, pointer)
        await wait_for(lambda: controller.execute.await_count == 2)
        action, params = controller.execute.await_args.args
        assert action == "touch_actions"
        assert wda_timeline(params["actions"]) == [
            (0, "pointerDown", 0), (1, "pointerDown", 180),
            (0, "pointerMove", 300), (1, "pointerMove", 400),
            (0, "pointerUp", 800), (1, "pointerUp", 900),
        ]
        assert all(item.get("x", 100) == 100 and item.get("y", 200) == 200 for item in params["actions"])
        assert channel.sent == []
    finally:
        await channels.close()


@pytest.mark.parametrize("move_at,up_at", [(.4, 1.9), (2.5, 3.75)])
async def test_single_drag_keeps_full_move_and_hold_before_lift(clock, move_at, up_at):
    controller = AsyncMock()
    controller.execute.return_value = {"width": 390, "height": 844}
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        for elapsed, action in [(0, 0), (move_at, 2), (up_at, 1)]:
            send_touch(channel, clock, elapsed, action)
        await wait_for(lambda: controller.execute.await_count == 2)
        actions = controller.execute.await_args.args[1]["actions"]
        assert wda_timeline(actions) == [(0, "pointerDown", 0), (0, "pointerMove", round(move_at * 1000)),
                                         (0, "pointerUp", round(up_at * 1000))]
    finally:
        await channels.close()


async def test_geometry_await_and_queued_packets_keep_original_arrival_schedule(clock):
    started, release = asyncio.Event(), asyncio.Event()
    gestures = []

    async def execute(action, params=None):
        if action == "screen_geometry":
            started.set()
            await release.wait()
            return {"width": 390, "height": 844}
        assert action == "touch_actions"
        gestures.append(params["actions"])

    channels, channel = ChannelController(SimpleNamespace(execute=execute)), Channel()
    channels.attach(channel)
    try:
        send_touch(channel, clock, 0, 0)
        await started.wait()
        for elapsed, action, pointer in [(.2, 0, 1), (.6, 2, 0), (.8, 2, 1), (1.1, 1, 0), (1.4, 1, 1)]:
            send_touch(channel, clock, elapsed, action, pointer)
        clock.now = 105  # Geometry finishes after all gesture packets arrived.
        release.set()
        await wait_for(lambda: bool(gestures))
        assert len(gestures) == 1
        assert wda_timeline(gestures[0]) == [
            (0, "pointerDown", 0), (1, "pointerDown", 200),
            (0, "pointerMove", 600), (1, "pointerMove", 800),
            (0, "pointerUp", 1100), (1, "pointerUp", 1400),
        ]
    finally:
        await channels.close()


@pytest.mark.parametrize("invalid", ["missing-down", "duplicate-down", "six-fingers", "too-long", "zero-frame", "negative", "outside", "unsupported"])
async def test_invalid_touch_resets_gesture_without_dispatching_partial_input(clock, invalid):
    controller = AsyncMock()
    controller.execute.return_value = {"width": 390, "height": 844}
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        if invalid == "missing-down":
            send_touch(channel, clock, 0, 1)
        elif invalid == "zero-frame":
            send_touch(channel, clock, 0, 0, width=0)
        elif invalid == "negative":
            send_touch(channel, clock, 0, 0, x=-1)
            send_touch(channel, clock, .1, 1)
        elif invalid == "outside":
            send_touch(channel, clock, 0, 0, x=1170)
            send_touch(channel, clock, .1, 1)
        else:
            send_touch(channel, clock, 0, 0)
            if invalid == "duplicate-down":
                send_touch(channel, clock, .1, 0)
            elif invalid == "six-fingers":
                for pointer in range(1, 6):
                    send_touch(channel, clock, pointer / 10, 0, pointer)
            elif invalid == "too-long":
                send_touch(channel, clock, 30.001, 1)
            else:
                send_touch(channel, clock, .1, 3)
        await wait_for(lambda: bool(channel.sent))
        assert not any(call.args[0] == "touch_actions" for call in controller.execute.await_args_list)
        # A subsequent valid gesture starts fresh; no earlier pointer is replayed.
        send_touch(channel, clock, 31, 0, 9)
        send_touch(channel, clock, 31.2, 1, 9)
        await wait_for(lambda: any(call.args[0] == "touch_actions" for call in controller.execute.await_args_list))
        gestures = [call.args[1]["actions"] for call in controller.execute.await_args_list if call.args[0] == "touch_actions"]
        assert len(gestures) == 1 and wda_timeline(gestures[0]) == [(9, "pointerDown", 0), (9, "pointerUp", 200)]
    finally:
        await channels.close()


async def test_touch_limits_accept_five_fingers_at_thirty_second_boundary(clock):
    controller = AsyncMock()
    controller.execute.return_value = {"width": 390, "height": 844}
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        for pointer in range(5):
            send_touch(channel, clock, pointer / 10, 0, pointer)
        for pointer in range(5):
            send_touch(channel, clock, 30, 1, pointer)
        await wait_for(lambda: controller.execute.await_count == 2)
        actions = controller.execute.await_args.args[1]["actions"]
        assert [event for event in wda_timeline(actions) if event[1] == "pointerUp"] == [
            (pointer, "pointerUp", 30000) for pointer in range(5)]
        assert channel.sent == []
    finally:
        await channels.close()


@pytest.mark.parametrize("moves,hold,allowed", [(4094, 0, True), (4095, 0, False), (4094, .1, False)])
async def test_action_budget_counts_generated_actions_before_dispatch(clock, moves, hold, allowed):
    controller = AsyncMock()
    controller.execute.return_value = {"width": 390, "height": 844}
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        send_touch(channel, clock, 0, 0)
        await asyncio.sleep(0)
        for index in range(1, moves + 1):
            send_touch(channel, clock, index / 1000, 2)
            await asyncio.sleep(0)  # Keep this within the separate 256-packet queue bound.
        send_touch(channel, clock, moves / 1000 + hold, 1)
        await wait_for(lambda: bool(channel.sent) or controller.execute.await_count == 2)
        gestures = [call.args[1]["actions"] for call in controller.execute.await_args_list if call.args[0] == "touch_actions"]
        if allowed:
            assert len(gestures) == 1 and len(gestures[0]) == 4096
            assert channel.sent == []
        else:
            assert gestures == [] and "4096" in json.loads(channel.sent[0])["error"]["message"]
    finally:
        await channels.close()


async def test_lost_gesture_response_is_not_replayed_and_next_gesture_is_independent(clock):
    effects = []

    async def execute(action, params=None):
        if action == "screen_geometry":
            return {"width": 390, "height": 844}
        effects.append(params["actions"])
        if len(effects) == 1:
            raise ConnectionError("response lost after effect")

    channels, channel = ChannelController(SimpleNamespace(execute=execute)), Channel()
    channels.attach(channel)
    try:
        send_touch(channel, clock, 0, 0)
        send_touch(channel, clock, .2, 1)
        await wait_for(lambda: bool(channel.sent))
        send_touch(channel, clock, .3, 1)  # A stray old lift cannot replay the failed gesture.
        await wait_for(lambda: len(channel.sent) == 2)
        send_touch(channel, clock, 1, 0, 9)
        send_touch(channel, clock, 1.4, 1, 9)
        await wait_for(lambda: len(effects) == 2)
        assert [wda_timeline(actions) for actions in effects] == [
            [(0, "pointerDown", 0), (0, "pointerUp", 200)],
            [(9, "pointerDown", 0), (9, "pointerUp", 400)],
        ]
    finally:
        await channels.close()


@pytest.mark.parametrize("geometry", [{"width": float("inf"), "height": 844}, {"width": True, "height": 844}, {"height": 844}])
async def test_invalid_geometry_cannot_dispatch_touch(clock, geometry):
    controller = AsyncMock()
    controller.execute.return_value = geometry
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        send_touch(channel, clock, 0, 0)
        send_touch(channel, clock, .2, 1)
        await wait_for(lambda: bool(channel.sent))
        assert not any(call.args[0] == "touch_actions" for call in controller.execute.await_args_list)
    finally:
        await channels.close()


async def test_datachannel_json_actions_pass_through_audited_controller():
    controller = AsyncMock()
    controller.execute.return_value = {"ok": True}
    channels = ChannelController(controller)
    channel = Channel()
    channels.attach(channel)
    try:
        channel.events["message"](json.dumps({"id": 7, "method": "portal.Tap", "params": {"x": 3, "y": 4}}))
        await wait_for(lambda: bool(channel.sent))
        controller.execute.assert_awaited_once_with("portal.Tap", {"x": 3, "y": 4})
        assert json.loads(channel.sent[0])["id"] == 7
    finally:
        await channels.close()


async def test_scrcpy_key_is_dispatched_once_and_bad_packets_do_not_dispatch():
    controller = AsyncMock()
    channels = ChannelController(controller)
    channel = Channel()
    channels.attach(channel)
    try:
        channel.events["message"](b"\x00" + struct.pack("<BIII", 0, 66, 0, 0))
        channel.events["message"](b"\x00" + struct.pack("<BIII", 1, 66, 0, 0))
        channel.events["message"](b"\x01\x00")
        await wait_for(lambda: bool(channel.sent))
        controller.execute.assert_awaited_once_with("key", {"key_code": 66})
        assert "error" in json.loads(channel.sent[0])
    finally:
        await channels.close()


def scroll(x=300, y=600, width=1170, height=2532, horizontal=1, vertical=-2, timestamp=None):
    # The scroll encoder and decoder use little-endian
    # fields. Its optional trailing field is a frame timestamp, not a display ID.
    packet = b"\x03" + struct.pack("<iiHHhhI", x, y, width, height, horizontal, vertical, 0)
    return packet if timestamp is None else packet + struct.pack("<I", timestamp)


@pytest.mark.parametrize("timestamp", [None, 0x12345678])
async def test_scroll_packet_uses_fresh_geometry_and_bounded_wda_path(clock, timestamp):
    controller = AsyncMock()
    controller.execute.return_value = {"width": 390, "height": 844}
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        channel.events["message"](scroll(timestamp=timestamp))
        await wait_for(lambda: bool(channel.sent) or controller.execute.await_count == 2)
        assert channel.sent == []
        assert controller.execute.await_args_list[0].args == ("screen_geometry",)
        action, params = controller.execute.await_args.args
        assert action == "touch_actions"
        assert params["actions"] == [
            {"type": "pointerMove", "x": 100, "y": 200},
            {"type": "pointerDown", "x": 100, "y": 200},
            {"type": "pointerMove", "x": 130, "y": 140, "duration": 200},
            {"type": "pause", "duration": 120},
            {"type": "pointerUp", "x": 130, "y": 140},
        ]
    finally:
        await channels.close()


async def test_scroll_clamps_raw_signed_delta_and_refreshes_geometry(clock):
    controller = AsyncMock()
    controller.execute.side_effect = [{"width": 390, "height": 844}, None,
                                      {"width": 844, "height": 390}, None]
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        channel.events["message"](scroll(horizontal=32767, vertical=-32768))
        await wait_for(lambda: bool(channel.sent) or controller.execute.await_count == 2)
        channel.events["message"](scroll(horizontal=32767, vertical=-32768))
        await wait_for(lambda: bool(channel.sent) or controller.execute.await_count == 4)
        assert channel.sent == []
        paths = [call.args[1]["actions"] for call in controller.execute.await_args_list
                 if call.args[0] == "touch_actions"]
        assert len(paths) == 2
        assert (paths[0][2]["x"], paths[0][2]["y"]) == (389, 0)
        assert paths[1][0]["x"] == pytest.approx(300 * 844 / 1170)
        assert paths[1][2]["x"] == pytest.approx(300 * 844 / 1170 + 300)
        assert paths[1][2]["y"] == 0
    finally:
        await channels.close()


@pytest.mark.parametrize("packet", [b"\x03", scroll()[:-1], scroll()+b"\x00",
    scroll()+bytes(5), scroll(width=0), scroll(height=0), scroll(x=-1),
    scroll(x=1170), scroll(y=2532)])
async def test_malformed_scroll_has_no_input_or_geometry_request(clock, packet):
    controller = AsyncMock()
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        channel.events["message"](packet)
        await wait_for(lambda: bool(channel.sent))
        controller.execute.assert_not_awaited()
    finally:
        await channels.close()


async def test_zero_scroll_does_not_synthesize_a_tap(clock):
    controller = AsyncMock()
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        channel.events["message"](scroll(horizontal=0, vertical=0))
        channel.events["message"](b"\x03")  # Barrier after the zero packet.
        await wait_for(lambda: bool(channel.sent))
        assert len(channel.sent) == 1
        controller.execute.assert_not_awaited()
    finally:
        await channels.close()


async def test_scroll_expiring_during_geometry_read_does_not_dispatch(clock):
    async def execute(action, params=None):
        assert action == "screen_geometry"
        clock.now += 30.001
        return {"width": 390, "height": 844}

    controller = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        channel.events["message"](scroll())
        await wait_for(lambda: bool(channel.sent))
        controller.execute.assert_awaited_once_with("screen_geometry")
        assert "expired" in json.loads(channel.sent[0])["error"]["message"].lower()
    finally:
        await channels.close()


async def test_scroll_response_loss_does_not_replay_and_next_scroll_gets_new_geometry(clock):
    controller = AsyncMock()
    controller.execute.side_effect = [{"width": 390, "height": 844}, ConnectionError("lost response"),
                                      {"width": 390, "height": 844}, None]
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        channel.events["message"](scroll())
        await wait_for(lambda: bool(channel.sent))
        assert controller.execute.await_count == 2
        channel.events["message"](scroll())
        await wait_for(lambda: controller.execute.await_count == 4)
        assert [call.args[0] for call in controller.execute.await_args_list] == [
            "screen_geometry", "touch_actions", "screen_geometry", "touch_actions"]
        assert len(channel.sent) == 1
    finally:
        await channels.close()


async def test_binary_keydown_passes_modifiers_once_ignores_up_and_repeat_count():
    controller = AsyncMock()
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        channel.events["message"](b"\x00" + struct.pack("<BIII", 0, 29, 100, 0x100001))
        channel.events["message"](b"\x00" + struct.pack("<BIII", 1, 29, 100, 0x100001))
        channel.events["message"](b"\x00" + struct.pack("<BIII", 2, 29, 100, 0x100001))
        channel.events["message"](b"\x00")  # Barrier after ignored non-down events.
        await wait_for(lambda: bool(channel.sent))
        controller.execute.assert_awaited_once_with("key", {"key_code": 29, "metaState": 0x100001})
        assert len(channel.sent) == 1
    finally:
        await channels.close()


async def test_public_encoder_key_text_and_clipboard_literal_wire_fixtures():
    # Literal wire bytes for key, text and clipboard messages. Key/text
    # fields are LE; clipboard lengths and acknowledgement sequence stay BE.
    controller = AsyncMock()
    controller.execute.return_value = "héλ"
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        for packet in [
            "00 00 1d000000 00000000 01000000",
            "01 05000000 68c3a9cebb",
            "09 0102030405060708 01 00000005 68c3a9cebb",
            "08 00",
        ]:
            channel.events["message"](bytes.fromhex(packet))
        reply = bytes.fromhex("00 00000005 68c3a9cebb")
        await wait_for(lambda: reply in channel.sent)
        assert [call.args for call in controller.execute.await_args_list] == [
            ("key", {"key_code": 29, "metaState": 1}),
            ("type_text", {"text": "héλ"}),
            ("clipboard_set", {"text": "héλ"}),
            ("type_text", {"text": "héλ"}),
            ("clipboard_get",),
        ]
        assert channel.sent == [bytes.fromhex("01 0102030405060708"), reply]
    finally:
        await channels.close()


@pytest.mark.parametrize("timestamp", [b"", bytes.fromhex("78563412")])
async def test_public_touch_literal_fixture_preserves_opaque_pointer_identity_and_timing(clock, timestamp):
    # React writes an opaque pointer BE and all numeric touch fields LE. The
    # decoder reads that pointer LE; its stable identity is preserved.
    packets = [
        "02 00 0102030405060708 2c010000 58020000 9204 e409 ffff 01000000 01000000",
        "02 00 1112131415161718 2c010000 58020000 9204 e409 ffff 01000000 01000000",
        "02 01 0102030405060708 2c010000 58020000 9204 e409 ffff 01000000 01000000",
        "02 01 1112131415161718 2c010000 58020000 9204 e409 ffff 01000000 01000000",
    ]
    controller = AsyncMock()
    controller.execute.return_value = {"width": 390, "height": 844}
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        for elapsed, packet in zip([0, .1, .4, .5], packets):
            clock.now = 100 + elapsed
            channel.events["message"](bytes.fromhex(packet) + timestamp)
        await wait_for(lambda: bool(channel.sent) or controller.execute.await_count == 2)
        assert channel.sent == []
        actions = controller.execute.await_args.args[1]["actions"]
        first, second = 0x0807060504030201, 0x1817161514131211
        assert wda_timeline(actions) == [
            (first, "pointerDown", 0), (second, "pointerDown", 100),
            (first, "pointerUp", 400), (second, "pointerUp", 500),
        ]
        assert all(action.get("x", 100) == 100 and action.get("y", 200) == 200 for action in actions)
    finally:
        await channels.close()


@pytest.mark.parametrize("packet", [
    b"\x00" + bytes(12), b"\x00" + bytes(14),
    bytes.fromhex("01 050000"), bytes.fromhex("01 05000000 68c3a9"),
    bytes.fromhex("01 01000000 ff"), bytes.fromhex("01 00000001 61"),
    touch(0)[:-1], touch(0) + bytes(1), touch(0) + bytes(3), touch(0) + bytes(5),
    bytes.fromhex("09 0102030405060708 00 01000000 61"),
])
async def test_truncated_or_malformed_wire_packet_cannot_dispatch(packet):
    controller = AsyncMock()
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        channel.events["message"](packet)
        await wait_for(lambda: bool(channel.sent))
        controller.execute.assert_not_awaited()
        assert "error" in json.loads(channel.sent[0])
    finally:
        await channels.close()


async def test_scroll_expired_in_queue_does_not_request_geometry(clock):
    controller = AsyncMock()
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        channel.events["message"](scroll(timestamp=0xFFFFFFFF))
        clock.now += 30.001
        await wait_for(lambda: bool(channel.sent))
        controller.execute.assert_not_awaited()
        assert "expired" in json.loads(channel.sent[0])["error"]["message"].lower()
    finally:
        await channels.close()


@pytest.mark.parametrize("geometry", [
    {"width": 0, "height": 844}, {"width": 1, "height": 844},
    {"width": float("inf"), "height": 844}, {"width": float("nan"), "height": 844},
    {"width": True, "height": 844}, {"width": 390}, None,
])
async def test_invalid_scroll_geometry_cannot_dispatch_touch(clock, geometry):
    controller = AsyncMock()
    controller.execute.return_value = geometry
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        channel.events["message"](scroll())
        await wait_for(lambda: bool(channel.sent))
        controller.execute.assert_awaited_once_with("screen_geometry")
    finally:
        await channels.close()


async def test_scroll_cannot_interleave_incomplete_gesture_and_recovers_fresh(clock):
    controller = AsyncMock()
    controller.execute.return_value = {"width": 390, "height": 844}
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        send_touch(channel, clock, 0, 0)
        channel.events["message"](scroll())
        send_touch(channel, clock, .1, 1)  # The rejected gesture cannot replay.
        await wait_for(lambda: len(channel.sent) == 2)
        controller.execute.assert_awaited_once_with("screen_geometry")
        channel.events["message"](scroll())
        await wait_for(lambda: controller.execute.await_count == 3)
        assert [call.args[0] for call in controller.execute.await_args_list] == [
            "screen_geometry", "screen_geometry", "touch_actions"]
    finally:
        await channels.close()


async def test_scroll_clipped_to_same_point_does_not_synthesize_tap(clock):
    controller = AsyncMock()
    controller.execute.return_value = {"width": 390, "height": 844}
    channels, channel = ChannelController(controller), Channel()
    channels.attach(channel)
    try:
        channel.events["message"](scroll(x=0, y=0, horizontal=-1, vertical=-1))
        channel.events["message"](b"\x03")  # Barrier after the no-op packet.
        await wait_for(lambda: bool(channel.sent))
        assert len(channel.sent) == 1
        controller.execute.assert_awaited_once_with("screen_geometry")
    finally:
        await channels.close()
