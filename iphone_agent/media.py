"""Owned WDA H.264/Opus streams, local recordings, and WebRTC.

WDA's raw video is Annex-B or length-framed scrcpy H.264/H.265. Its audio is
Opus; only scrcpy framing has packet boundaries. This module exposes those
streams through per-session USB forwarding. WebRTC and MP4 recording decode
scrcpy-framed H.264 by default; WDA screenshots are an explicit fallback.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
import logging
from pathlib import Path
import struct
import time
from typing import Any
import uuid

import av
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaRecorder, MediaRelay
from aiortc.mediastreams import MediaStreamError
from aiortc.sdp import candidate_from_sdp

from .transport import USBForward


_CONFIG_FLAG = 1 << 63
_KEYFRAME_FLAG = 1 << 62
_PTS_MASK = (1 << 62) - 1
# WDA's nominal connected-stop path waits 5s, then up to 20s for its alert.
# This bounds the client cleanup wait, not the remote iOS operation.
BROADCAST_CLEANUP_TIMEOUT = 30.0
_log = logging.getLogger(__name__)


class _ScrcpyPackets:
    """Read complete scrcpy packets, independent of TCP read boundaries."""

    def __init__(self, reader: asyncio.StreamReader, maximum: int):
        self.reader = reader
        self.maximum = maximum

    async def read(self) -> tuple[bool, bool, int, bytes]:
        header = await self.reader.readexactly(12)
        flags_pts, size = struct.unpack(">QI", header)
        if not 0 < size <= self.maximum:
            raise RuntimeError(f"Invalid scrcpy packet size: {size}")
        payload = await self.reader.readexactly(size)
        return bool(flags_pts & _CONFIG_FLAG), bool(flags_pts & _KEYFRAME_FLAG), flags_pts & _PTS_MASK, payload


class RawVideoTrack(VideoStreamTrack):
    """Decode WDA scrcpy-framed H.264 access units through PyAV."""

    def __init__(self, reader: asyncio.StreamReader):
        super().__init__()
        self.packets = _ScrcpyPackets(reader, 16 * 1024 * 1024)
        self.decoder = av.CodecContext.create("h264", "r")
        self.pending: list[av.VideoFrame] = []
        self.prefetched: av.VideoFrame | None = None
        self._config: bytes = b""
        self._needs_config = False
        self.frames = 0
        self.first_frame = asyncio.Event()
        self.error: str | None = None

    async def prepare(self) -> None:
        self.prefetched = await asyncio.wait_for(self._decode_next(), timeout=10)

    async def _decode_next(self) -> av.VideoFrame:
        while True:
            if self.pending:
                return self.pending.pop(0)
            config, keyframe, pts_us, payload = await self.packets.read()
            if config:
                # WDA sends SPS/PPS separately. Annex-B decoders need those
                # parameter sets beside a complete access unit.
                self._config = payload
                self._needs_config = True
                continue
            if self._config and (self._needs_config or keyframe):
                payload = self._config + payload
                self._needs_config = False
            frames = self.decoder.decode(av.Packet(payload))
            for index, frame in enumerate(frames):
                frame.pts = int(pts_us * 90 / 1000) + index
                frame.time_base = Fraction(1, 90000)
                self.pending.append(frame)

    async def recv(self) -> av.VideoFrame:
        if self.readyState != "live":
            raise MediaStreamError
        try:
            if self.prefetched is not None:
                frame, self.prefetched = self.prefetched, None
            else:
                frame = await self._decode_next()
        except Exception as exc:
            self.error = str(exc)
            self.stop()
            raise MediaStreamError from exc
        self.frames += 1
        self.first_frame.set()
        return frame


class RawAudioTrack(AudioStreamTrack):
    """Decode 48 kHz Opus packets with the supplied OpusHead config."""

    def __init__(self, reader: asyncio.StreamReader):
        super().__init__()
        self.packets = _ScrcpyPackets(reader, 1024 * 1024)
        self.decoder = av.CodecContext.create("opus", "r")
        self.pending: list[av.AudioFrame] = []
        self.prefetched: av.AudioFrame | None = None
        self.frames = 0
        self.first_frame = asyncio.Event()
        self.error: str | None = None

    async def prepare(self) -> None:
        self.prefetched = await asyncio.wait_for(self._decode_next(), timeout=10)

    async def _decode_next(self) -> av.AudioFrame:
        while True:
            if self.pending:
                return self.pending.pop(0)
            config, _keyframe, pts_us, payload = await self.packets.read()
            if config:
                if not payload.startswith(b"OpusHead"):
                    raise RuntimeError("Invalid OpusHead config packet")
                self.decoder.extradata = payload
                continue
            frames = self.decoder.decode(av.Packet(payload))
            for index, frame in enumerate(frames):
                frame.pts = int(pts_us * 48 / 1000) + index * frame.samples
                frame.time_base = Fraction(1, 48000)
                self.pending.append(frame)

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        try:
            if self.prefetched is not None:
                frame, self.prefetched = self.prefetched, None
            else:
                frame = await self._decode_next()
        except Exception as exc:
            self.error = str(exc)
            self.stop()
            raise MediaStreamError from exc
        self.frames += 1
        self.first_frame.set()
        return frame


class ScreenshotTrack(VideoStreamTrack):
    """Poll WDA screenshots and decode them into real video frames."""

    def __init__(self, wda: Any, fps: int = 5):
        super().__init__()
        if not 1 <= fps <= 15:
            raise ValueError("screenshot frame rate must be between 1 and 15")
        self.wda = wda
        self.fps = fps
        self._period = 1 / fps
        self._next_at = 0.0
        self._pts = 0
        self._prefetched: av.VideoFrame | None = None
        self.first_frame = asyncio.Event()
        self.frames = 0
        self.error: str | None = None

    async def prepare(self) -> None:
        """Fail before reporting success if the screenshot source is unavailable."""
        self._prefetched = await self._capture()

    async def _capture(self) -> av.VideoFrame:
        encoded = await self.wda.screenshot()
        raw = base64.b64decode(encoded, validate=True)
        with av.open(BytesIO(raw), mode="r") as container:
            frame = next(container.decode(video=0), None)
        if frame is None:
            raise RuntimeError("WDA screenshot did not contain a video frame")
        return frame

    async def recv(self) -> av.VideoFrame:
        if self.readyState != "live":
            raise MediaStreamError
        now = asyncio.get_running_loop().time()
        if self._next_at > now:
            await asyncio.sleep(self._next_at - now)
        self._next_at = max(self._next_at + self._period, asyncio.get_running_loop().time())
        try:
            if self._prefetched is not None:
                frame, self._prefetched = self._prefetched, None
            else:
                frame = await self._capture()
        except Exception as exc:
            self.error = str(exc)
            self.stop()
            raise MediaStreamError from exc
        frame.pts = self._pts
        frame.time_base = Fraction(1, 90000)
        self._pts += round(90000 / self.fps)
        self.frames += 1
        self.first_frame.set()
        return frame


@dataclass
class _RawStream:
    info: dict[str, Any]
    forward: Any | None = None
    stop_attempted: bool = False
    remote_stopped: bool = False


@dataclass
class _Peer:
    connection: RTCPeerConnection
    track: VideoStreamTrack
    audio_track: AudioStreamTrack | None
    stats_track: VideoStreamTrack
    sources: tuple[str, ...]
    touched: float


@dataclass
class _Recording:
    recorder: MediaRecorder
    tracks: list[VideoStreamTrack | AudioStreamTrack]
    stats_track: VideoStreamTrack
    sources: tuple[str, ...]
    path: Path
    started: float


@dataclass
class _InternalSource:
    info: dict[str, Any]
    forward: Any | None = None
    writer: asyncio.StreamWriter | None = None
    track: RawVideoTrack | RawAudioTrack | None = None
    users: int = 0
    stopping: bool = True
    stop_attempted: bool = False
    remote_stopped: bool = False


class MediaManager:
    def __init__(self, wda: Any, udid: str, output_dir: str | Path,
                 connection_factory: Any | None = None):
        if not udid:
            raise ValueError("An explicit USB device UDID is required")
        self.wda = wda
        self.udid = udid
        self._connection_factory = connection_factory or USBForward
        self.output_dir = Path(output_dir).expanduser().resolve()
        self._video: dict[int, _RawStream] = {}
        self._audio: dict[int, _RawStream] = {}
        self._internal: dict[str, _InternalSource] = {}
        self._relay = MediaRelay()
        self._peers: dict[str, _Peer] = {}
        self._recording: _Recording | None = None
        self._broadcast_owned = False
        self._lock = asyncio.Lock()
        self.datachannel_handler: Any | None = None
        self._reaper_task: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        """A recording, stream or WebRTC peer is running, which makes no calls of its own."""
        return self._recording is not None or bool(self._video or self._audio or self._peers)

    @staticmethod
    def _id(p: dict[str, Any]) -> int:
        value = p.get("id", p.get("sessionId"))
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
            raise ValueError("A numeric stream id is required")
        return int(value)

    @staticmethod
    def _peer_id(p: dict[str, Any]) -> str:
        value = p.get("sessionId", p.get("peerId", p.get("peer_id")))
        if not isinstance(value, str) or not value:
            raise ValueError("A WebRTC sessionId is required")
        return value

    @staticmethod
    def _stream_spec(kind: str, p: dict[str, Any]) -> dict[str, Any]:
        if kind == "video":
            allowed = ("width", "height", "codec", "framing", "bitrate", "quality", "fps", "maxPixels", "port")
            body = {key: p[key] for key in allowed if key in p}
            body.setdefault("width", 390)
            body.setdefault("height", 844)
        else:
            allowed = ("codec", "framing", "bitrate", "channels", "port")
            body = {key: p[key] for key in allowed if key in p}
            # Raw Opus has no packet boundaries on TCP. Always request scrcpy framing.
            body["framing"] = "scrcpy"
        return body

    async def _start_stream(self, kind: str, p: dict[str, Any]) -> dict[str, Any]:
        body = self._stream_spec(kind, p)
        root = f"/mobilerun/{'screen' if kind == 'video' else 'audio'}capture"
        info = await self.wda.call("POST", root + "/start", body)
        if not isinstance(info, dict) or not isinstance(info.get("id"), int) or not isinstance(info.get("port"), int):
            raise RuntimeError(f"WDA returned invalid {kind} stream information")
        stream_id = info["id"]
        store = self._video if kind == "video" else self._audio
        owned = store[stream_id] = _RawStream(info)
        try:
            owned.forward = self._connection_factory(self.udid, info["port"])
            await owned.forward.open()
        except BaseException as original:
            errors = [original]
            try:
                await self._stop_stream(kind, stream_id)
            except BaseException as exc:
                errors.extend(exc.exceptions if isinstance(exc, BaseExceptionGroup) else [exc])
            if len(errors) > 1:
                raise BaseExceptionGroup("Stream startup and cleanup failed", errors) from original
            raise
        forward = owned.forward
        return {**info, "localPort": forward.port, "host": getattr(forward, "host", "127.0.0.1"),
                "audioRequiresBroadcast": kind == "audio"}

    async def _stop_remote(self, kind: str, owned: _RawStream | _InternalSource) -> Any:
        if owned.remote_stopped:
            return None
        root = f"/mobilerun/{'screen' if kind == 'video' else 'audio'}capture"
        path = f"{root}/{owned.info['id']}"
        if owned.stop_attempted:
            # A previous stop may have succeeded before its response was lost.
            # WDA rejects stopping absent IDs, but GET confirms their absence.
            status = await self.wda.call("GET", path)
            if status is None:
                owned.remote_stopped = True
                return None
            if not isinstance(status, dict) or status.get("id") != owned.info["id"]:
                raise RuntimeError(f"WDA returned invalid {kind} stream status during cleanup")
        owned.stop_attempted = True
        result = await self.wda.call("POST", path + "/stop")
        owned.remote_stopped = True
        return result

    async def _stop_stream(self, kind: str, stream_id: int) -> dict[str, Any]:
        store = self._video if kind == "video" else self._audio
        owned = store.get(stream_id)
        if owned is None:
            raise ValueError(f"{kind} stream {stream_id} was not created by this controller")
        errors: list[BaseException] = []
        result = None
        try:
            result = await self._stop_remote(kind, owned)
        except BaseException as exc:
            errors.append(exc)
        if owned.forward is not None:
            try:
                await owned.forward.close()
            except BaseException as exc:
                errors.append(exc)
            else:
                owned.forward = None
        if owned.remote_stopped and owned.forward is None:
            del store[stream_id]
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup(f"Could not fully close {kind} stream {stream_id}", errors)
        return {"id": stream_id, "stopped": True, "wda": result}

    async def _list_streams(self, kind: str) -> Any:
        root = f"/mobilerun/{'screen' if kind == 'video' else 'audio'}capture"
        result = await self.wda.call("GET", root)
        if not isinstance(result, dict) or not isinstance(result.get("sessions"), list):
            raise RuntimeError(f"WDA returned invalid {kind} sessions")
        store = self._video if kind == "video" else self._audio
        return {"sessions": [
            {**entry, **({"localPort": store[entry["id"]].forward.port,
                         "host": getattr(store[entry["id"]].forward, "host", "127.0.0.1")}
                       if entry.get("id") in store and store[entry["id"]].forward is not None else {})}
            for entry in result["sessions"] if isinstance(entry, dict)
        ]}

    async def _get_stream(self, kind: str, stream_id: int) -> Any:
        root = f"/mobilerun/{'screen' if kind == 'video' else 'audio'}capture"
        result = await self.wda.call("GET", f"{root}/{stream_id}")
        store = self._video if kind == "video" else self._audio
        if isinstance(result, dict) and stream_id in store and store[stream_id].forward is not None:
            return {**result, "localPort": store[stream_id].forward.port,
                    "host": getattr(store[stream_id].forward, "host", "127.0.0.1")}
        return result

    async def _acquire_raw(self, kind: str, p: dict[str, Any]) -> _InternalSource:
        existing = self._internal.get(kind)
        if existing is not None:
            if existing.stopping:
                await self._release_raw(kind)
            else:
                existing.users += 1
                return existing
        if kind == "video":
            body = {"width": p.get("width", 390), "height": p.get("height", 844),
                    "codec": "h264", "framing": "scrcpy", "fps": p.get("fps", 15)}
            root = "/mobilerun/screencapture"
        else:
            broadcast = await self.wda.call("GET", "/mobilerun/screencapture/broadcast")
            if not isinstance(broadcast, dict) or broadcast.get("state") != "connected":
                raise RuntimeError("ReplayKit app-audio broadcast must be active before requesting audio")
            body = {"codec": "opus", "framing": "scrcpy", "channels": p.get("channels", 2)}
            root = "/mobilerun/audiocapture"
        info = await self.wda.call("POST", root + "/start", body)
        if not isinstance(info, dict) or not isinstance(info.get("id"), int) or not isinstance(info.get("port"), int):
            raise RuntimeError(f"WDA returned invalid {kind} stream information")
        source = self._internal[kind] = _InternalSource(info)
        try:
            source.forward = self._connection_factory(self.udid, info["port"])
            await source.forward.open()
            reader, source.writer = await asyncio.open_connection(
                getattr(source.forward, "host", "127.0.0.1"), source.forward.port)
            source.track = RawVideoTrack(reader) if kind == "video" else RawAudioTrack(reader)
            try:
                await source.track.prepare()
            except TimeoutError as exc:
                raise RuntimeError(f"No {kind} packets received from WDA stream") from exc
            source.users = 1
            source.stopping = False
            return source
        except BaseException as original:
            errors = [original, *await self._cleanup_raw(kind, source)]
            if len(errors) > 1:
                raise BaseExceptionGroup("Raw stream startup and cleanup failed", errors) from original
            raise

    async def _cleanup_raw(self, kind: str, source: _InternalSource) -> list[BaseException]:
        errors: list[BaseException] = []
        if source.track is not None:
            try:
                source.track.stop()
            except BaseException as exc:
                errors.append(exc)
            else:
                source.track = None
        if source.writer is not None:
            writer_errors: list[BaseException] = []
            try:
                source.writer.close()
            except BaseException as exc:
                writer_errors.append(exc)
            try:
                await source.writer.wait_closed()
            except BaseException as exc:
                writer_errors.append(exc)
            if not writer_errors:
                source.writer = None
            errors.extend(writer_errors)
        if source.forward is not None:
            try:
                await source.forward.close()
            except BaseException as exc:
                errors.append(exc)
            else:
                source.forward = None
        try:
            await self._stop_remote(kind, source)
        except BaseException as exc:
            errors.append(exc)
        if source.remote_stopped and source.track is None and source.writer is None and source.forward is None:
            del self._internal[kind]
        return errors

    async def _release_raw(self, kind: str, *, force: bool = False) -> None:
        source = self._internal.get(kind)
        if source is None:
            return
        if force:
            source.users = 0
        elif not source.stopping:
            source.users = max(0, source.users - 1)
        if source.users > 0:
            return
        source.stopping = True
        errors = await self._cleanup_raw(kind, source)
        if errors:
            raise BaseExceptionGroup(f"Could not fully close raw {kind} stream", errors)

    async def _broadcast_start(self, p: dict[str, Any]) -> Any:
        existing = await self.wda.call("GET", "/mobilerun/screencapture/broadcast")
        if not isinstance(existing, dict) or existing.get("state") not in ("idle", "connected", "paused"):
            raise RuntimeError("WDA returned invalid broadcast status")
        if existing["state"] != "idle":
            return existing
        body = {key: p[key] for key in ("timeout", "confirmButtonLabels", "dismissButtonLabels",
                                       "goToApplicationButtonLabels", "restoreForegroundApp") if key in p}
        self._broadcast_owned = True
        try:
            result = await self.wda.call("POST", "/mobilerun/screencapture/broadcast/start", body)
            if not isinstance(result, dict) or result.get("state") not in ("connected", "paused"):
                raise RuntimeError("WDA returned invalid broadcast start status")
        except BaseException as original:
            try:
                async with asyncio.timeout(BROADCAST_CLEANUP_TIMEOUT):
                    # An idle extension cannot prove that failed startup's iOS capture
                    # ended. Keep the intent so an explicit stop or close can retry.
                    await self._broadcast_stop(p, retain_ownership=True)
            except BaseException as cleanup_error:
                raise BaseExceptionGroup("Broadcast startup and cleanup failed",
                                         [original, cleanup_error]) from original
            raise
        return result

    async def _broadcast_stop(self, p: dict[str, Any], *, retain_ownership: bool = False) -> Any:
        if not self._broadcast_owned:
            raise ValueError("Broadcast was not started by this controller")
        body = {key: p[key] for key in ("dismissButtonLabels", "goToApplicationButtonLabels") if key in p}
        result = await self.wda.call("POST", "/mobilerun/screencapture/broadcast/stop", body)
        if not isinstance(result, dict) or result.get("state") != "idle":
            raise RuntimeError("WDA returned invalid broadcast stop status")
        if not retain_ownership:
            self._broadcast_owned = False
        return result

    async def _offer(self, p: dict[str, Any]) -> dict[str, Any]:
        offer = p.get("offer", p)
        if not isinstance(offer, dict) or not isinstance(offer.get("sdp"), str):
            raise ValueError("An SDP offer is required")
        if offer.get("type", "offer") != "offer":
            raise ValueError("SDP type must be offer")
        fallback = p.get("videoSource") == "screenshot"
        sources: list[str] = []
        if fallback:
            stats_track = ScreenshotTrack(self.wda, p.get("fps", 5))
            await stats_track.prepare()
            track = stats_track
        else:
            video = await self._acquire_raw("video", p)
            sources.append("video")
            stats_track = video.track
            track = self._relay.subscribe(video.track, buffered=False)
        audio_track: AudioStreamTrack | None = None
        if p.get("audio"):
            try:
                audio = await self._acquire_raw("audio", p)
                sources.append("audio")
                audio_track = self._relay.subscribe(audio.track, buffered=False)
            except BaseException:
                track.stop()
                for kind in reversed(sources):
                    await self._release_raw(kind)
                raise
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peer_id = uuid.uuid4().hex
        @peer.on("datachannel")
        def on_datachannel(channel: Any) -> None:
            if self.datachannel_handler is not None:
                self.datachannel_handler(channel)

        @peer.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            if peer.connectionState in ("closed", "failed"):
                async with self._lock:
                    if peer_id in self._peers:
                        try:
                            await self._close_peer(peer_id)
                        except Exception:
                            _log.exception("Could not close failed WebRTC peer %s", peer_id)
        try:
            peer.addTrack(track)
            if audio_track is not None:
                peer.addTrack(audio_track)
            await peer.setRemoteDescription(RTCSessionDescription(sdp=offer["sdp"], type="offer"))
            answer = await peer.createAnswer()
            await peer.setLocalDescription(answer)
        except BaseException:
            track.stop()
            if audio_track is not None:
                audio_track.stop()
            await peer.close()
            for kind in reversed(sources):
                await self._release_raw(kind)
            raise
        self._peers[peer_id] = _Peer(peer, track, audio_track, stats_track, tuple(sources), time.monotonic())
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_idle_peers(), name="webrtc-peer-reaper")
        return {"sessionId": peer_id, "peerId": peer_id, "type": peer.localDescription.type,
                "sdp": peer.localDescription.sdp,
                "videoSource": "wda-screenshot" if fallback else "wda-h264", "audio": audio_track is not None}

    async def _ice(self, p: dict[str, Any]) -> dict[str, Any]:
        peer_id = self._peer_id(p)
        peer = self._peers.get(peer_id)
        if peer is None:
            raise ValueError("Unknown WebRTC peer")
        value = p.get("candidate")
        if value is None:
            await peer.connection.addIceCandidate(None)
        else:
            if isinstance(value, dict):
                candidate_data = value
            elif isinstance(value, str):
                candidate_data = {"candidate": value, "sdpMid": p.get("sdpMid"),
                                  "sdpMLineIndex": p.get("sdpMLineIndex")}
            else:
                raise ValueError("ICE candidate must be a string or object")
            if not isinstance(candidate_data.get("candidate"), str):
                raise ValueError("ICE candidate must contain a candidate string")
            text = candidate_data["candidate"].removeprefix("candidate:")
            try:
                candidate = candidate_from_sdp(text)
            except (ValueError, IndexError, AssertionError) as exc:
                raise ValueError("Invalid ICE candidate") from exc
            candidate.sdpMid = candidate_data.get("sdpMid")
            candidate.sdpMLineIndex = candidate_data.get("sdpMLineIndex")
            if candidate.sdpMid is None and candidate.sdpMLineIndex is None:
                raise ValueError("ICE candidate requires sdpMid or sdpMLineIndex")
            await peer.connection.addIceCandidate(candidate)
        peer.touched = time.monotonic()
        return {"accepted": True}

    async def _keepalive(self, p: dict[str, Any]) -> dict[str, Any]:
        peer = self._peers.get(self._peer_id(p))
        if peer is None:
            raise ValueError("Unknown WebRTC peer")
        peer.touched = time.monotonic()
        return {"state": peer.connection.connectionState, "videoFrames": peer.stats_track.frames,
                "error": peer.stats_track.error}

    async def _requestframe(self, p: dict[str, Any]) -> dict[str, Any]:
        peer = self._peers.get(self._peer_id(p))
        if peer is None:
            raise ValueError("Unknown WebRTC peer")
        senders = [sender for sender in peer.connection.getSenders() if sender.track is peer.track]
        if not senders:
            raise RuntimeError("WebRTC peer has no video sender")
        # Pinned aiortc 1.15.0 exposes no public sender keyframe request method.
        senders[0]._send_keyframe()
        peer.touched = time.monotonic()
        return {"requested": True}

    async def _close_peer(self, peer_id: str) -> dict[str, Any]:
        peer = self._peers.pop(peer_id, None)
        if peer is None:
            raise ValueError("Unknown WebRTC peer")
        peer.track.stop()
        if peer.audio_track is not None:
            peer.audio_track.stop()
        errors: list[Exception] = []
        try:
            await peer.connection.close()
        except Exception as exc:
            errors.append(exc)
        for kind in reversed(peer.sources):
            try:
                await self._release_raw(kind)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup(f"Could not fully close WebRTC peer {peer_id}", errors)
        return {"sessionId": peer_id, "closed": True}

    async def _reap_idle_peers(self) -> None:
        while True:
            await asyncio.sleep(30)
            async with self._lock:
                now = time.monotonic()
                expired = [peer_id for peer_id, peer in self._peers.items() if now - peer.touched > 120]
                for peer_id in expired:
                    try:
                        await self._close_peer(peer_id)
                    except Exception:
                        _log.exception("Could not close idle WebRTC peer %s", peer_id)
                if not self._peers:
                    return

    async def _recording_start(self, p: dict[str, Any]) -> dict[str, Any]:
        if self._recording is not None:
            raise RuntimeError("A recording is already active")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"recording-{uuid.uuid4().hex}.mp4"
        fallback = p.get("videoSource") == "screenshot"
        sources: list[str] = []
        if fallback:
            stats_track = ScreenshotTrack(self.wda, p.get("fps", 5))
            await stats_track.prepare()
            video_track = stats_track
        else:
            video = await self._acquire_raw("video", p)
            sources.append("video")
            stats_track = video.track
            video_track = self._relay.subscribe(video.track, buffered=False)
        tracks: list[VideoStreamTrack | AudioStreamTrack] = [video_track]
        if p.get("audio"):
            try:
                audio = await self._acquire_raw("audio", p)
                sources.append("audio")
                tracks.append(self._relay.subscribe(audio.track, buffered=False))
            except BaseException:
                video_track.stop()
                for kind in reversed(sources):
                    await self._release_raw(kind)
                raise
        recorder: MediaRecorder | None = None
        try:
            recorder = MediaRecorder(str(path), format="mp4")
            for track in tracks:
                recorder.addTrack(track)
            await recorder.start()
            await asyncio.wait_for(stats_track.first_frame.wait(), timeout=10)
            if stats_track.error:
                raise RuntimeError(f"Recording source failed: {stats_track.error}")
        except BaseException:
            for track in tracks:
                track.stop()
            if recorder is not None:
                await recorder.stop()
            path.unlink(missing_ok=True)
            for kind in reversed(sources):
                await self._release_raw(kind)
            raise
        assert recorder is not None
        self._recording = _Recording(recorder, tracks, stats_track, tuple(sources), path, time.time())
        return {"recording": True, "path": str(path),
                "videoSource": "wda-screenshot" if fallback else "wda-h264", "audio": len(tracks) > 1}

    async def _recording_stop(self) -> dict[str, Any]:
        recording, self._recording = self._recording, None
        if recording is None:
            raise RuntimeError("No recording is active")
        for track in recording.tracks:
            track.stop()
        source_error = recording.stats_track.error
        errors: list[Exception] = []
        try:
            await recording.recorder.stop()
        except Exception as exc:
            errors.append(exc)
        for kind in reversed(recording.sources):
            try:
                await self._release_raw(kind)
            except Exception as exc:
                errors.append(exc)
        if source_error:
            errors.append(RuntimeError(f"Recording source failed: {source_error}"))
        if errors:
            raise ExceptionGroup("Recording stop failed", errors)
        return {"recording": False, "path": str(recording.path),
                "size": recording.path.stat().st_size, "videoFrames": recording.stats_track.frames}

    async def dispatch(self, action: str, p: dict[str, Any]) -> Any:
        async with self._lock:
            if action == "stream_video_start":
                return await self._start_stream("video", p)
            if action == "stream_audio_start":
                return await self._start_stream("audio", p)
            if action in ("stream_video_stop", "stream_audio_stop"):
                return await self._stop_stream(action.split("_")[1], self._id(p))
            if action in ("stream_video_list", "stream_audio_list"):
                return await self._list_streams(action.split("_")[1])
            if action in ("stream_video_get", "stream_audio_get"):
                return await self._get_stream(action.split("_")[1], self._id(p))
            if action == "stream_video_keyframe":
                stream_id = self._id(p)
                if stream_id not in self._video:
                    raise ValueError("Video stream was not created by this controller")
                return await self.wda.call("POST", f"/mobilerun/screencapture/{stream_id}/keyframe")
            if action == "stream_broadcast_status":
                return await self.wda.call("GET", "/mobilerun/screencapture/broadcast")
            if action == "stream_broadcast_start":
                return await self._broadcast_start(p)
            if action == "stream_broadcast_stop":
                return await self._broadcast_stop(p)
            if action == "recording_start":
                return await self._recording_start(p)
            if action == "recording_stop":
                return await self._recording_stop()
            if action == "recording_status":
                recording = self._recording
                return ({"recording": False} if recording is None else
                        {"recording": recording.stats_track.error is None,
                         "path": str(recording.path), "videoFrames": recording.stats_track.frames,
                         "error": recording.stats_track.error})
            if action == "webrtc_offer":
                return await self._offer(p)
            if action == "webrtc_ice":
                return await self._ice(p)
            if action == "webrtc_keepalive":
                return await self._keepalive(p)
            if action == "webrtc_requestframe":
                return await self._requestframe(p)
            if action == "webrtc_close":
                return await self._close_peer(self._peer_id(p))
            if action == "webrtc_configuration":
                return {"iceServers": [], "videoSource": "wda-h264", "screenshotFallback": True,
                        "audio": "conditional", "audioRequires": "ReplayKit app-audio broadcast consent"}
            raise ValueError(f"Unsupported media action: {action}")

    async def close(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            await asyncio.gather(self._reaper_task, return_exceptions=True)
            self._reaper_task = None
        async with self._lock:
            errors: list[BaseException] = []
            if self._recording is not None:
                try:
                    await self._recording_stop()
                except BaseException as exc:
                    errors.append(exc)
            for peer_id in list(self._peers):
                try:
                    await self._close_peer(peer_id)
                except BaseException as exc:
                    errors.append(exc)
            for kind, store in (("video", self._video), ("audio", self._audio)):
                for stream_id in list(store):
                    try:
                        await self._stop_stream(kind, stream_id)
                    except BaseException as exc:
                        errors.append(exc)
            for kind in list(self._internal):
                try:
                    await self._release_raw(kind, force=True)
                except BaseException as exc:
                    errors.append(exc)
            if self._broadcast_owned:
                try:
                    await self._broadcast_stop({})
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                raise BaseExceptionGroup("Media cleanup failed", errors)
