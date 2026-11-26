"""Media routes and live aiortc / PyAV behavior with a stub screenshot source."""

import asyncio
import base64
from fractions import Fraction
from io import BytesIO
import re
import struct
import sys
from pathlib import Path

from PIL import Image
import av
import pytest
from aiortc import RTCConfiguration, RTCPeerConnection

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from iphone_agent import media as module  # noqa: E402
from iphone_agent.network import NetworkBindingError  # noqa: E402


def packet(payload, pts=0, config=False, keyframe=False):
    flags_pts = pts | ((1 << 63) if config else 0) | ((1 << 62) if keyframe else 0)
    return struct.pack(">QI", flags_pts, len(payload)) + payload


def h264_frame():
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width = encoder.height = 128
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, 15)
    encoder.options = {"tune": "zerolatency", "preset": "ultrafast"}
    encoder.open()
    frame = av.VideoFrame.from_image(Image.new("RGB", (128, 128), "red")).reformat(format="yuv420p")
    return bytes(encoder.encode(frame)[0])


def opus_frame():
    encoder = av.CodecContext.create("libopus", "w")
    encoder.sample_rate = 48000
    encoder.layout = "stereo"
    encoder.format = "s16"
    encoder.time_base = Fraction(1, 48000)
    encoder.open()
    frame = av.AudioFrame(format="s16", layout="stereo", samples=960)
    frame.sample_rate = 48000
    frame.pts = 0
    frame.time_base = Fraction(1, 48000)
    for plane in frame.planes:
        plane.update(bytes(plane.buffer_size))
    return encoder.extradata, bytes(encoder.encode(frame)[0])


def local_peer():
    """The default configuration asks a public STUN server, which takes seconds and needs the internet."""
    return RTCPeerConnection(RTCConfiguration(iceServers=[]))


class WDA:
    def __init__(self):
        self.calls = []
        self.next_id = 10
        self.sessions = {"screencapture": {}, "audiocapture": {}}
        image = Image.new("RGB", (128, 128), "blue")
        blob = BytesIO()
        image.save(blob, format="PNG")
        self.image = base64.b64encode(blob.getvalue()).decode()

    async def screenshot(self):
        return self.image

    async def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/mobilerun/screencapture/broadcast":
            return {"state": "idle"}
        if path.endswith("/broadcast/start"):
            return {"state": "connected"}
        if path.endswith("/broadcast/stop"):
            return {"state": "idle"}
        kind = "screencapture" if "screencapture" in path else "audiocapture"
        base = f"/mobilerun/{kind}"
        if path == base + "/start":
            self.next_id += 1
            result = {"id": self.next_id, "port": 9000 + self.next_id, **body}
            self.sessions[kind][self.next_id] = result
            return result
        if path == base:
            return {"sessions": list(self.sessions[kind].values())}
        if path.endswith("/stop"):
            stream_id = int(path.rsplit("/", 2)[-2])
            self.sessions[kind].pop(stream_id)
            return {"id": stream_id}
        if path.endswith("/keyframe"):
            return {"requested": True}
        if path.startswith(base + "/"):
            return self.sessions[kind].get(int(path.rsplit("/", 1)[-1]))
        raise AssertionError(path)


class Forward:
    instances = []

    def __init__(self, udid, remote_port):
        self.udid = udid
        self.remote_port = remote_port
        self.port = remote_port + 10000
        self.closed = False
        Forward.instances.append(self)

    async def open(self):
        return self

    async def close(self):
        self.closed = True


@pytest.fixture
def manager(monkeypatch, tmp_path):
    Forward.instances.clear()
    monkeypatch.setattr(module, "USBForward", Forward)
    wda = WDA()
    return module.MediaManager(wda, "abc", tmp_path)


@pytest.mark.asyncio
async def test_owned_video_and_audio_streams(manager):
    video = await manager.dispatch("stream_video_start", {"width": 390, "height": 844})
    assert video["localPort"] == video["port"] + 10000
    assert Forward.instances[0].udid == "abc"
    assert (await manager.dispatch("stream_video_list", {}))["sessions"][0]["id"] == video["id"]
    assert await manager.dispatch("stream_video_keyframe", {"id": video["id"]}) == {"requested": True}
    audio = await manager.dispatch("stream_audio_start", {"framing": "raw"})
    assert audio["framing"] == "scrcpy"
    assert audio["audioRequiresBroadcast"]
    assert ("POST", "/mobilerun/audiocapture/start", {"framing": "scrcpy"}) in manager.wda.calls
    with pytest.raises(ValueError, match="not created"):
        await manager.dispatch("stream_video_stop", {"id": 999})
    await manager.close()
    assert all(item.closed for item in Forward.instances)
    assert manager.wda.sessions["screencapture"] == {}
    assert manager.wda.sessions["audiocapture"] == {}
    assert video["id"] != audio["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["construction", "open", "close"])
async def test_stream_startup_failure_stops_remote_capture_and_closes_connection(tmp_path, failure):
    closed = []

    class FailedConnection:
        async def open(self):
            raise RuntimeError("connection failed")

        async def close(self):
            closed.append(True)
            if failure == "close" and len(closed) == 1:
                raise RuntimeError("close failed")

    def factory(udid, port):
        if failure == "construction":
            raise ValueError("factory failed")
        return FailedConnection()

    manager = module.MediaManager(WDA(), "abc", tmp_path, connection_factory=factory)
    expected = BaseExceptionGroup if failure == "close" else (RuntimeError, ValueError)
    with pytest.raises(expected):
        await manager.dispatch("stream_video_start", {})
    assert manager.wda.sessions["screencapture"] == {}
    assert closed == ([] if failure == "construction" else [True])
    if failure == "close":
        assert len(manager._video) == 1
        assert next(iter(manager._video.values())).remote_stopped
        await manager.close()
        assert closed == [True, True]
    assert manager._video == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["open", "prepare"])
async def test_raw_startup_cleanup_attempts_remote_stop_after_local_close_errors(monkeypatch, tmp_path, failure):
    closed = []
    cleanup_errors = True

    class Connection:
        host, port = "::1", 9000

        async def open(self):
            if failure == "open":
                raise RuntimeError("open failed")
            return self

        async def close(self):
            closed.append("connection")
            if cleanup_errors:
                raise RuntimeError("connection close failed")

    class Writer:
        def close(self):
            closed.append("writer")

        async def wait_closed(self):
            if cleanup_errors:
                raise RuntimeError("writer close failed")

    class Track:
        def __init__(self, reader):
            pass

        async def prepare(self):
            raise RuntimeError("prepare failed")

        def stop(self):
            closed.append("track")

    async def connect(host, port):
        return asyncio.StreamReader(), Writer()

    monkeypatch.setattr(module.asyncio, "open_connection", connect)
    monkeypatch.setattr(module, "RawVideoTrack", Track)
    manager = module.MediaManager(WDA(), "abc", tmp_path, connection_factory=lambda *_: Connection())
    with pytest.raises(BaseExceptionGroup) as caught:
        await manager._acquire_raw("video", {})
    assert len(caught.value.exceptions) == (2 if failure == "open" else 3)
    assert closed == (["connection"] if failure == "open" else ["track", "writer", "connection"])
    assert manager.wda.sessions["screencapture"] == {}
    assert manager._internal["video"].stopping and manager._internal["video"].users == 0
    assert manager._internal["video"].remote_stopped
    cleanup_errors = False
    await manager.close()
    assert manager._internal == {}


@pytest.mark.asyncio
async def test_direct_connection_factory_reports_ipv6_and_reads_that_host(monkeypatch, tmp_path):
    connections = []

    class DirectConnection:
        def __init__(self, udid, remote_port):
            self.udid = udid
            self.host = "::1"
            self.port = remote_port
            self.closed = False
            connections.append(self)

        async def open(self):
            return self

        async def close(self):
            self.closed = True

    class Writer:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    opened = []

    async def open_connection(host, port):
        opened.append((host, port))
        reader = asyncio.StreamReader()
        reader.feed_data(packet(h264_frame(), pts=10_000, keyframe=True))
        return reader, Writer()

    monkeypatch.setattr(module.asyncio, "open_connection", open_connection)
    manager = module.MediaManager(WDA(), "abc", tmp_path, connection_factory=DirectConnection)
    started = await manager.dispatch("stream_video_start", {})
    assert started["host"] == "::1"
    assert started["localPort"] == started["port"]
    listed = await manager.dispatch("stream_video_list", {})
    assert listed["sessions"][0]["host"] == "::1"
    fetched = await manager.dispatch("stream_video_get", {"id": started["id"]})
    assert fetched["host"] == "::1"
    await manager.dispatch("stream_video_stop", {"id": started["id"]})
    source = await manager._acquire_raw("video", {})
    assert opened == [("::1", source.info["port"])]
    await manager._release_raw("video")
    assert all(connection.closed for connection in connections)


class FailedStopWDA(WDA):
    def __init__(self):
        super().__init__()
        self.stop_error = NetworkBindingError("binding preflight failed")
        self.fail_after_effect = False
        self.fail_id = None

    async def call(self, method, path, body=None):
        if path == "/mobilerun/screencapture/broadcast":
            self.calls.append((method, path, body))
            return {"state": "connected"}
        fail = (path.endswith("/stop") and "/broadcast/" not in path and self.stop_error is not None
                and (self.fail_id is None or path.endswith(f"/{self.fail_id}/stop")))
        if fail and not self.fail_after_effect:
            self.calls.append((method, path, body))
            raise self.stop_error
        result = await super().call(method, path, body)
        if fail:
            raise self.stop_error
        return result


@pytest.fixture
def raw_parts(monkeypatch):
    state = {"prepare_error": None, "local_error": None, "closed": []}

    class Connection(Forward):
        async def close(self):
            state["closed"].append("forward")
            if state["local_error"] == "forward":
                raise RuntimeError("forward close failed")
            await super().close()

    class Writer:
        def close(self):
            state["closed"].append("writer close")
            if state["local_error"] == "writer close":
                raise RuntimeError("writer close failed")

        async def wait_closed(self):
            state["closed"].append("writer wait")
            if state["local_error"] == "writer wait":
                raise RuntimeError("writer wait failed")

    class Track:
        def __init__(self, reader):
            self.stopped = False

        async def prepare(self):
            if state["prepare_error"] is not None:
                raise state["prepare_error"]

        def stop(self):
            state["closed"].append("track")
            self.stopped = True
            if state["local_error"] == "track":
                raise RuntimeError("track stop failed")

    async def connect(host, port):
        return asyncio.StreamReader(), Writer()

    monkeypatch.setattr(module.asyncio, "open_connection", connect)
    monkeypatch.setattr(module, "RawVideoTrack", Track)
    monkeypatch.setattr(module, "RawAudioTrack", Track)
    state["factory"] = Connection
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["video", "audio"])
@pytest.mark.parametrize("retry", ["stop", "close"])
async def test_stream_failed_stop_keeps_remote_ownership_for_retry(tmp_path, kind, retry):
    wda = FailedStopWDA()
    manager = module.MediaManager(wda, "abc", tmp_path, connection_factory=Forward)
    stream = await manager.dispatch(f"stream_{kind}_start", {})
    with pytest.raises(NetworkBindingError):
        await manager.dispatch(f"stream_{kind}_stop", {"id": stream["id"]})
    store = manager._video if kind == "video" else manager._audio
    remote = wda.sessions["screencapture" if kind == "video" else "audiocapture"]
    assert stream["id"] in store and stream["id"] in remote
    assert Forward.instances[-1].closed
    wda.stop_error = None
    if retry == "stop":
        await manager.dispatch(f"stream_{kind}_stop", {"id": stream["id"]})
    else:
        await manager.close()
    assert store == remote == {}


@pytest.mark.asyncio
async def test_owned_stop_retry_recognizes_remote_stop_after_lost_response(tmp_path):
    wda = FailedStopWDA()
    wda.fail_after_effect = True
    wda.stop_error = TimeoutError("stop response lost")
    manager = module.MediaManager(wda, "abc", tmp_path, connection_factory=Forward)
    stream = await manager.dispatch("stream_video_start", {})
    with pytest.raises(TimeoutError):
        await manager.dispatch("stream_video_stop", {"id": stream["id"]})
    assert wda.sessions["screencapture"] == {}
    wda.stop_error = None
    assert (await manager.dispatch("stream_video_stop", {"id": stream["id"]}))["stopped"]
    assert manager._video == {}
    assert sum(path.endswith(f"/{stream['id']}/stop") for _, path, _ in wda.calls) == 1


@pytest.mark.asyncio
async def test_stream_startup_failed_remote_cleanup_is_owned_until_close(tmp_path):
    wda = FailedStopWDA()

    def factory(*args):
        raise RuntimeError("connection construction failed")

    manager = module.MediaManager(wda, "abc", tmp_path, connection_factory=factory)
    with pytest.raises(BaseExceptionGroup):
        await manager.dispatch("stream_video_start", {})
    assert set(manager._video) == set(wda.sessions["screencapture"]) != set()
    wda.stop_error = None
    await manager.close()
    assert manager._video == wda.sessions["screencapture"] == {}


@pytest.mark.asyncio
async def test_raw_startup_failed_cleanup_is_not_reused_on_next_acquire(tmp_path, raw_parts):
    wda = FailedStopWDA()
    raw_parts["prepare_error"] = RuntimeError("track prepare failed")
    manager = module.MediaManager(wda, "abc", tmp_path, connection_factory=raw_parts["factory"])
    with pytest.raises(BaseExceptionGroup):
        await manager._acquire_raw("video", {})
    old = manager._internal["video"]
    assert old.users == 0 and old.stopping
    assert old.info["id"] in wda.sessions["screencapture"]
    started = wda.next_id
    with pytest.raises(BaseExceptionGroup):
        await manager._acquire_raw("video", {})
    assert wda.next_id == started and old.users == 0
    raw_parts["prepare_error"] = None
    wda.stop_error = None
    new = await manager._acquire_raw("video", {})
    assert new is not old and new.info["id"] != old.info["id"]
    assert new.users == 1 and not new.stopping
    await manager._release_raw("video")
    assert manager._internal == wda.sessions["screencapture"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("local_error", [None, "track", "writer close", "writer wait", "forward"])
async def test_raw_last_release_keeps_failed_cleanup_and_refcount_consistent(tmp_path, raw_parts, local_error):
    wda = FailedStopWDA()
    manager = module.MediaManager(wda, "abc", tmp_path, connection_factory=raw_parts["factory"])
    source = await manager._acquire_raw("video", {})
    assert await manager._acquire_raw("video", {}) is source
    await manager._release_raw("video")
    assert source.users == 1 and raw_parts["closed"] == []
    raw_parts["local_error"] = local_error
    with pytest.raises(BaseExceptionGroup):
        await manager._release_raw("video")
    assert source.users == 0 and source.stopping
    assert source.info["id"] in wda.sessions["screencapture"]
    assert set(raw_parts["closed"]) == {"track", "writer close", "writer wait", "forward"}
    with pytest.raises(BaseExceptionGroup):
        await manager._acquire_raw("video", {})
    assert source.users == 0 and wda.next_id == source.info["id"]
    raw_parts["local_error"] = None
    wda.stop_error = None
    await manager.close()
    assert manager._internal == wda.sessions["screencapture"] == {}


@pytest.mark.asyncio
async def test_close_drains_orphan_internal_sources_and_continues_after_failure(tmp_path, raw_parts):
    wda = FailedStopWDA()
    manager = module.MediaManager(wda, "abc", tmp_path, connection_factory=raw_parts["factory"])
    video = await manager._acquire_raw("video", {})
    audio = await manager._acquire_raw("audio", {})
    await manager._acquire_raw("video", {})
    wda.fail_id = video.info["id"]
    with pytest.raises(BaseExceptionGroup, match="Media cleanup failed"):
        await manager.close()
    assert video.users == 0 and video.stopping
    assert manager._internal == {"video": video}
    assert audio.info["id"] not in wda.sessions["audiocapture"]
    wda.stop_error = None
    await manager.close()
    assert manager._internal == wda.sessions["screencapture"] == wda.sessions["audiocapture"] == {}


@pytest.mark.asyncio
async def test_broadcast_only_owned_stop(manager):
    with pytest.raises(ValueError, match="not started"):
        await manager.dispatch("stream_broadcast_stop", {})
    assert (await manager.dispatch("stream_broadcast_start", {}))["state"] == "connected"
    assert (await manager.dispatch("stream_broadcast_stop", {}))["state"] == "idle"


class BroadcastWDA(WDA):
    def __init__(self, state="idle"):
        super().__init__()
        self.state = state
        self.start_error = None
        self.stop_error = None
        self.start_result = {"state": "connected"}
        self.stop_result = {"state": "idle"}
        self.start_entered = asyncio.Event()
        self.start_release = None
        self.stop_release = None

    async def call(self, method, path, body=None):
        if not path.startswith("/mobilerun/screencapture/broadcast"):
            return await super().call(method, path, body)
        self.calls.append((method, path, body))
        if path.endswith("/start"):
            self.state = "connected"
            self.start_entered.set()
            if self.start_release is not None:
                await self.start_release.wait()
            if self.start_error is not None:
                raise self.start_error
            return self.start_result
        if path.endswith("/stop"):
            if self.stop_release is not None:
                await self.stop_release.wait()
            if self.stop_error is not None:
                raise self.stop_error
            self.state = self.stop_result.get("state") if isinstance(self.stop_result, dict) else None
            return self.stop_result
        return {"state": self.state}


@pytest.mark.asyncio
async def test_broadcast_timeout_after_start_effect_attempts_owned_stop(tmp_path):
    wda = BroadcastWDA()
    original = wda.start_error = TimeoutError("start response timed out")
    manager = module.MediaManager(wda, "abc", tmp_path)
    with pytest.raises(TimeoutError) as caught:
        await manager.dispatch("stream_broadcast_start", {})
    assert caught.value is original
    assert wda.state == "idle"
    assert wda.calls[-1][1].endswith("/broadcast/stop")
    assert manager._broadcast_owned
    await manager.close()
    assert not manager._broadcast_owned


@pytest.mark.asyncio
async def test_broadcast_cancellation_after_start_effect_attempts_owned_stop(tmp_path):
    wda = BroadcastWDA()
    wda.start_release = asyncio.Event()
    manager = module.MediaManager(wda, "abc", tmp_path)
    task = asyncio.create_task(manager.dispatch("stream_broadcast_start", {}))
    await wda.start_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wda.state == "idle"
    assert wda.calls[-1][1].endswith("/broadcast/stop")
    assert manager._broadcast_owned
    await manager.dispatch("stream_broadcast_stop", {})
    assert not manager._broadcast_owned


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", ["stop", "close"])
@pytest.mark.parametrize("failure", ["timeout", "cancel"])
async def test_broadcast_failed_start_cleanup_preserves_errors_and_allows_retry(tmp_path, retry, failure):
    wda = BroadcastWDA()
    original = wda.start_error = (TimeoutError("start response timed out") if failure == "timeout"
                                  else asyncio.CancelledError("start request cancelled"))
    cleanup_error = wda.stop_error = RuntimeError("stop response failed")
    manager = module.MediaManager(wda, "abc", tmp_path)
    with pytest.raises(BaseExceptionGroup) as caught:
        await manager.dispatch("stream_broadcast_start", {})
    assert caught.value.exceptions == (original, cleanup_error)
    assert manager._broadcast_owned
    assert wda.state == "connected"
    wda.stop_error = None
    if retry == "stop":
        await manager.dispatch("stream_broadcast_stop", {})
    else:
        await manager.close()
    assert wda.state == "idle"
    assert not manager._broadcast_owned


@pytest.mark.asyncio
async def test_broadcast_failed_start_cleanup_has_a_client_deadline(monkeypatch, tmp_path):
    wda = BroadcastWDA()
    original = wda.start_error = TimeoutError("start response timed out")
    wda.stop_release = asyncio.Event()
    manager = module.MediaManager(wda, "abc", tmp_path)
    monkeypatch.setattr(module, "BROADCAST_CLEANUP_TIMEOUT", 0.01)
    with pytest.raises(BaseExceptionGroup) as caught:
        await asyncio.wait_for(manager.dispatch("stream_broadcast_start", {}), timeout=1)
    assert caught.value.exceptions[0] is original
    assert isinstance(caught.value.exceptions[1], TimeoutError)
    assert manager._broadcast_owned
    wda.stop_release.set()
    await manager.close()
    assert wda.state == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["connected", "paused"])
@pytest.mark.parametrize("failure", [False, True])
async def test_preexisting_broadcast_is_never_claimed_or_stopped(tmp_path, state, failure):
    wda = BroadcastWDA(state)
    wda.start_result = {"state": state}
    manager = module.MediaManager(wda, "abc", tmp_path)
    if failure:
        wda.start_error = TimeoutError("existing start response timed out")
    assert (await manager.dispatch("stream_broadcast_start", {}))["state"] == state
    with pytest.raises(ValueError, match="not started"):
        await manager.dispatch("stream_broadcast_stop", {})
    await manager.close()
    assert wda.calls == [("GET", "/mobilerun/screencapture/broadcast", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, [], {}, {"state": "unknown"}, {"state": []}])
async def test_broadcast_bad_preflight_status_has_no_start_effect(tmp_path, status):
    wda = WDA()

    async def call(method, path, body=None):
        wda.calls.append((method, path, body))
        return status

    wda.call = call
    manager = module.MediaManager(wda, "abc", tmp_path)
    with pytest.raises(RuntimeError, match="broadcast status"):
        await manager.dispatch("stream_broadcast_start", {})
    assert wda.calls == [("GET", "/mobilerun/screencapture/broadcast", None)]
    assert not manager._broadcast_owned


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {}, {"state": "idle"}, {"state": "unknown"}])
async def test_broadcast_invalid_start_response_attempts_owned_cleanup(tmp_path, result):
    wda = BroadcastWDA()
    wda.start_result = result
    manager = module.MediaManager(wda, "abc", tmp_path)
    with pytest.raises(RuntimeError, match="broadcast start"):
        await manager.dispatch("stream_broadcast_start", {})
    assert wda.state == "idle"
    assert manager._broadcast_owned
    await manager.close()
    assert not manager._broadcast_owned


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {}, {"state": "connected"}, {"state": "paused"}])
async def test_broadcast_invalid_stop_response_keeps_owned_cleanup_retry(tmp_path, result):
    wda = BroadcastWDA()
    manager = module.MediaManager(wda, "abc", tmp_path)
    await manager.dispatch("stream_broadcast_start", {})
    wda.stop_result = result
    with pytest.raises(RuntimeError, match="broadcast stop"):
        await manager.dispatch("stream_broadcast_stop", {})
    assert manager._broadcast_owned
    wda.stop_result = {"state": "idle"}
    await manager.close()
    assert not manager._broadcast_owned


@pytest.mark.asyncio
async def test_webrtc_audio_requires_live_replaykit(manager):
    client = local_peer()
    try:
        client.addTransceiver("video", direction="recvonly")
        client.addTransceiver("audio", direction="recvonly")
        offer = await client.createOffer()
        await client.setLocalDescription(offer)
        with pytest.raises(RuntimeError, match="ReplayKit app-audio"):
            await manager.dispatch("webrtc_offer", {"sdp": client.localDescription.sdp,
                                                     "audio": True, "videoSource": "screenshot"})
        assert manager.wda.sessions["audiocapture"] == {}
    finally:
        await manager.close()
        await client.close()


@pytest.mark.asyncio
async def test_recording_outputs_real_mp4(manager):
    result = await manager.dispatch("recording_start", {"fps": 5, "videoSource": "screenshot"})
    assert result["recording"]
    await asyncio.sleep(0.3)
    stopped = await manager.dispatch("recording_stop", {})
    assert stopped["videoFrames"] >= 1
    assert stopped["size"] > 0
    assert Path(stopped["path"]).read_bytes()[4:8] == b"ftyp"
    assert (await manager.dispatch("recording_status", {}))["recording"] is False


@pytest.mark.asyncio
async def test_aiortc_offer_answer_and_ice(manager):
    client = local_peer()
    ready = asyncio.Event()

    @client.on("connectionstatechange")
    def connection_state_changed():
        if client.connectionState in {"connected", "failed", "closed"}:
            ready.set()

    try:
        client.addTransceiver("video", direction="recvonly")
        offer = await client.createOffer()
        await client.setLocalDescription(offer)
        answer = await manager.dispatch("webrtc_offer", {"sdp": client.localDescription.sdp,
                                                          "type": "offer", "videoSource": "screenshot"})
        assert answer["type"] == "answer"
        assert answer["videoSource"] == "wda-screenshot"
        await client.setRemoteDescription(module.RTCSessionDescription(sdp=answer["sdp"], type="answer"))
        await asyncio.wait_for(ready.wait(), timeout=5)
        assert client.connectionState == "connected"
        assert (await manager.dispatch("webrtc_keepalive", {"sessionId": answer["sessionId"]}))["videoFrames"] >= 0
        assert (await manager.dispatch("webrtc_requestframe", {"sessionId": answer["sessionId"]}))["requested"]
        assert (await manager.dispatch("webrtc_ice", {"sessionId": answer["sessionId"], "candidate": None}))["accepted"]
        assert (await manager.dispatch("webrtc_configuration", {}))["audio"] == "conditional"
    finally:
        await manager.close()
        await client.close()


@pytest.mark.asyncio
async def test_scrcpy_decoder_handles_fragmented_tcp_headers_and_payloads():
    reader = asyncio.StreamReader()
    stream = packet(h264_frame(), pts=40_000)
    reader.feed_data(stream[:3])
    reader.feed_data(stream[3:10])
    reader.feed_data(stream[10:])
    track = module.RawVideoTrack(reader)
    frame = await asyncio.wait_for(track.recv(), timeout=2)
    assert frame.width == frame.height == 128
    assert frame.pts == 3600
    assert frame.time_base == Fraction(1, 90000)
    track.stop()


@pytest.mark.asyncio
async def test_h264_separate_sps_pps_config_and_idr_access_unit():
    encoded = h264_frame()
    starts = list(re.finditer(b"\x00\x00\x00\x01|\x00\x00\x01", encoded))
    units = [encoded[match.start():(starts[index + 1].start() if index + 1 < len(starts) else len(encoded))]
             for index, match in enumerate(starts)]
    # Find NAL type after its Annex-B start code, preserving SEI + IDR in the AU.
    def nal_type(unit):
        offset = 4 if unit.startswith(b"\x00\x00\x00\x01") else 3
        return unit[offset] & 31

    config = b"".join(unit for unit in units if nal_type(unit) in (7, 8))
    access_unit = b"".join(unit for unit in units if nal_type(unit) not in (7, 8))
    assert config and access_unit
    reader = asyncio.StreamReader()
    reader.feed_data(packet(config, config=True) + packet(access_unit, pts=40_000, keyframe=True))
    track = module.RawVideoTrack(reader)
    frame = await asyncio.wait_for(track.recv(), timeout=2)
    assert frame.width == 128
    assert frame.pts == 3600
    track.stop()


@pytest.mark.asyncio
async def test_opushead_and_packet_decode():
    config, data = opus_frame()
    reader = asyncio.StreamReader()
    reader.feed_data(packet(config, config=True) + packet(data, pts=20_000))
    track = module.RawAudioTrack(reader)
    frame = await asyncio.wait_for(track.recv(), timeout=2)
    assert frame.sample_rate == 48000
    assert frame.samples > 0
    assert frame.pts == 960
    track.stop()


@pytest.mark.asyncio
async def test_webrtc_flat_candidate_shape(manager):
    class Connection:
        def __init__(self):
            self.received = None

        async def addIceCandidate(self, candidate):
            self.received = candidate

    connection = Connection()
    manager._peers["session"] = module._Peer(connection, None, None, None, (), 0.0)
    result = await manager.dispatch("webrtc_ice", {
        "sessionId": "session",
        "candidate": "candidate:1 1 udp 2130706431 192.0.2.1 12345 typ host",
        "sdpMid": "0", "sdpMLineIndex": 0,
    })
    assert result["accepted"]
    assert connection.received.sdpMid == "0"
    manager._peers.clear()


@pytest.mark.asyncio
async def test_default_recording_uses_decoded_raw_wda_stream(monkeypatch, tmp_path):
    video = h264_frame()
    connections = []

    async def serve(reader, writer):
        connections.append(writer)
        try:
            for index in range(30):
                writer.write(packet(video, pts=index * 66_667))
                await writer.drain()
                await asyncio.sleep(0.05)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    class PassthroughForward(Forward):
        def __init__(self, udid, remote_port):
            super().__init__(udid, remote_port)
            self.port = remote_port

    class RawWDA(WDA):
        async def call(self, method, path, body=None):
            result = await super().call(method, path, body)
            if path == "/mobilerun/screencapture/start":
                result["port"] = port
            return result

    monkeypatch.setattr(module, "USBForward", PassthroughForward)
    wda = RawWDA()
    manager = module.MediaManager(wda, "abc", tmp_path)
    try:
        started = await manager.dispatch("recording_start", {})
        assert started["videoSource"] == "wda-h264"
        await asyncio.sleep(0.25)
        stopped = await manager.dispatch("recording_stop", {})
        assert stopped["size"] > 0
        assert stopped["videoFrames"] >= 1
        assert wda.sessions["screencapture"] == {}
        assert connections
        client = local_peer()
        try:
            client.addTransceiver("video", direction="recvonly")
            offer = await client.createOffer()
            await client.setLocalDescription(offer)
            answer = await manager.dispatch("webrtc_offer", {"sdp": client.localDescription.sdp})
            assert answer["videoSource"] == "wda-h264"
            await client.setRemoteDescription(module.RTCSessionDescription(sdp=answer["sdp"], type="answer"))
            for _ in range(50):
                if client.connectionState == "connected":
                    break
                await asyncio.sleep(0.1)
            assert client.connectionState == "connected"
            assert (await manager.dispatch("webrtc_close", {"sessionId": answer["sessionId"]}))["closed"]
            assert wda.sessions["screencapture"] == {}
        finally:
            await client.close()
    finally:
        await manager.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_close_attempts_every_owned_stream_after_one_stop_failure(manager):
    first = await manager.dispatch("stream_video_start", {"width": 390, "height": 844})
    second = await manager.dispatch("stream_video_start", {"width": 390, "height": 844})
    original = manager.wda.call

    async def failing_call(method, path, body=None):
        result = await original(method, path, body)
        if path == f"/mobilerun/screencapture/{first['id']}/stop":
            raise RuntimeError("simulated stop failure")
        return result

    manager.wda.call = failing_call
    with pytest.raises(ExceptionGroup, match="Media cleanup failed"):
        await manager.close()
    assert all(forward.closed for forward in Forward.instances)
    assert manager.wda.sessions["screencapture"] == {}
    assert second["id"] not in manager._video
    manager.wda.call = original
    await manager.close()
    assert manager._video == {}
