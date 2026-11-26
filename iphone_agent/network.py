"""Bind an explicit WDA IP origin to a paired CoreDevice hardware UDID.

CoreDevice's JSON output is the identity source for network mode. The URL must
contain a literal IP, so neither DNS nor HTTP response metadata can select a
second device. USB remains the controller's default transport.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
from pathlib import Path
import signal
import tempfile
from urllib.parse import urlsplit

import aiohttp

from .transport import DirectConnection
from .wda import WDAClient


class NetworkBindingError(RuntimeError):
    """The requested network origin cannot be bound to the selected iPhone."""


class RecoverableNetworkError(NetworkBindingError):
    """Transport failed after identity validation; a new session may recover."""


class UsbLinkLost(RecoverableNetworkError):
    """The cable went away while a --network-fallback startup attempt waited for WDA."""


class CoreDeviceTimeoutError(RecoverableNetworkError):
    """One owned CoreDevice query exceeded its deadline and was reaped."""


class CoreDeviceUnavailableError(RecoverableNetworkError):
    """The exact paired device temporarily has no connected tunnel address."""


class NetworkAddressChangedError(RecoverableNetworkError):
    """The selected device's tunnel moved; the existing binding remains fixed."""


class DeveloperImageUnavailableError(RecoverableNetworkError):
    """Xcode's developer disk image did not mount; wireless links fail this transiently."""


class DeviceLockedError(RecoverableNetworkError):
    """The phone is locked, and the runner cannot start until its owner unlocks it."""


class DeveloperImageLockedError(DeveloperImageUnavailableError, DeviceLockedError):
    """iOS refuses to mount Xcode's developer disk image until the phone is unlocked."""


class WDATransportError(RecoverableNetworkError, ConnectionError):
    """A WDA response was lost; the requested action must not be replayed."""


def is_recoverable_error(error: BaseException) -> bool:
    """Identity failures and cancellation override transient grouped failures."""
    leaves = []

    def visit(exc):
        if isinstance(exc, BaseExceptionGroup):
            for child in exc.exceptions:
                visit(child)
        else:
            leaves.append(exc)

    visit(error)
    if any(isinstance(exc, asyncio.CancelledError) or
           (isinstance(exc, NetworkBindingError) and not isinstance(exc, RecoverableNetworkError))
           for exc in leaves):
        return False
    return any(isinstance(exc, RecoverableNetworkError) for exc in leaves)


def _parse_origin(url: str) -> tuple[str, str, int, ipaddress.IPv4Address | ipaddress.IPv6Address]:
    if not isinstance(url, str) or not url or "?" in url or "#" in url:
        raise ValueError("WDA URL must be an HTTP(S) IP origin without a query or fragment")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path:
            raise ValueError("WDA URL must be an HTTP(S) origin without a path")
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise ValueError("WDA URL must not contain credentials")
        host = parsed.hostname
        if host is None or "%" in host:
            raise ValueError("WDA URL must contain an unscoped literal IP address")
        address = ipaddress.ip_address(host)
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError("WDA URL port is out of range")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid WDA network origin: {exc}") from exc
    bracketed = f"[{address}]" if address.version == 6 else str(address)
    origin = f"{parsed.scheme}://{bracketed}:{port}"
    return origin, str(address), port, address


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.wait_for(process.wait(), timeout=2)


async def _coredevice_query_once(udid: str, timeout: float, kind: str) -> dict:
    """Run one fixed read-only CoreDevice query for the selected hardware."""
    if kind not in ("devices", "lockState"):
        raise ValueError("Unsupported CoreDevice query")
    with tempfile.TemporaryDirectory(prefix="iphone-agent-coredevice-") as directory:
        output = Path(directory) / f"{kind}.json"
        command = (["xcrun", "devicectl", "list", "devices"] if kind == "devices" else
                   ["xcrun", "devicectl", "device", "info", "lockState", "--device", udid])
        command += ["--json-output", str(output), "--quiet", "--timeout", str(max(1, int(timeout)))]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            status = await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError as exc:
            await _stop_process(process)
            raise CoreDeviceTimeoutError(f"CoreDevice {kind} query timed out") from exc
        except BaseException:
            await _stop_process(process)
            raise
        if status != 0:
            # The lock-state query wakes an inactive link at connect and exits nonzero while the phone
            # sleeps on Wi-Fi; the connect asks again. A failed inventory read stays a binding error.
            failure = CoreDeviceUnavailableError if kind == "lockState" else NetworkBindingError
            raise failure(f"CoreDevice {kind} query failed with exit status {status}")
        try:
            with output.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError) as exc:
            raise NetworkBindingError("CoreDevice did not write a valid query result") from exc
        if not isinstance(data, dict):
            raise NetworkBindingError("CoreDevice query result must be a JSON object")
        info = data.get("info")
        expected = "devicectl.list.devices" if kind == "devices" else "devicectl.device.info.lockState"
        if not isinstance(info, dict) or info.get("outcome") != "success":
            raise NetworkBindingError("CoreDevice query did not report success")
        if "commandType" in info and info["commandType"] != expected:
            raise NetworkBindingError("CoreDevice query returned an unexpected command type")
        return data


# kAMDMobileImageMounterDeviceLocked (0xE80000E2) anywhere in devicectl's error chain.
IMAGE_MOUNTER_DEVICE_LOCKED = -402652958


def _user_info(error, key):
    info = error.get("userInfo") if isinstance(error, dict) else None
    value = info.get(key) if isinstance(info, dict) else None
    return value if isinstance(value, dict) else {}


def _error_chain(error):
    """devicectl's error, then each nested NSUnderlyingError, outermost first."""
    while isinstance(error, dict):
        yield error
        error = _user_info(error, "NSUnderlyingError").get("error")


async def ensure_developer_image(udid: str, timeout: float) -> None:
    """Mount Xcode's developer disk image if it is not mounted yet.

    The image unmounts at every phone reboot and xcodebuild cannot launch WDA
    without it. Mount failures are recoverable, so startup retries until its
    deadline: a locked phone waits for the unlock, and paired-network links
    intermittently fail with assertion or device-property errors.
    """
    with tempfile.TemporaryDirectory(prefix="iphone-agent-coredevice-") as directory:
        output = Path(directory) / "ddiServices.json"
        process = await asyncio.create_subprocess_exec(
            "xcrun", "devicectl", "device", "info", "ddiServices", "--device", udid,
            "--json-output", str(output), "--quiet", "--timeout", str(max(1, int(timeout))),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
        try:
            status = await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError as exc:
            await _stop_process(process)
            raise CoreDeviceTimeoutError("Developer disk image query timed out") from exc
        except BaseException:
            await _stop_process(process)
            raise
        try:
            with output.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError) as exc:
            raise NetworkBindingError(f"Developer disk image query failed with exit status {status}") from exc
    if not isinstance(data, dict):
        raise NetworkBindingError("Developer disk image query result must be a JSON object")
    info = data.get("info")
    if status == 0 and isinstance(info, dict) and info.get("outcome") == "success":
        return
    chain = list(_error_chain(data.get("error")))
    if any(error.get("code") == IMAGE_MOUNTER_DEVICE_LOCKED for error in chain):
        raise DeveloperImageLockedError(
            "The iPhone is locked, so Xcode cannot mount its developer disk image; unlock the phone")
    # The outer descriptions are generic; the specific reason sits deeper in the chain.
    descriptions = [_user_info(error, "NSLocalizedDescription").get("string") for error in chain]
    reason = "; ".join(dict.fromkeys(text for text in descriptions if isinstance(text, str) and text))
    raise DeveloperImageUnavailableError(f"Developer disk image is unavailable: {reason or f'exit status {status}'}")


async def _coredevice_details_once(udid: str, timeout: float) -> dict:
    """Select one exact hardware record from a fresh OS inventory read."""
    data = await _coredevice_query_once(udid, timeout, "devices")
    result = data.get("result")
    devices = result.get("devices") if isinstance(result, dict) else None
    if not isinstance(devices, list):
        raise NetworkBindingError("CoreDevice returned no valid device inventory")
    hardware_ids, identifiers, active_addresses = set(), set(), set()
    selected = None
    for device in devices:
        if not isinstance(device, dict):
            raise NetworkBindingError("CoreDevice inventory contains a malformed device record")
        hardware, connection = device.get("hardwareProperties"), device.get("connectionProperties")
        hardware_id = hardware.get("udid") if isinstance(hardware, dict) else None
        identifier = device.get("identifier")
        if (not isinstance(hardware_id, str) or not hardware_id.strip()
                or not isinstance(identifier, str) or not identifier.strip()
                or not isinstance(connection, dict)
                or not all(isinstance(connection.get(key), str) and connection[key]
                           for key in ("pairingState", "tunnelState"))):
            raise NetworkBindingError("CoreDevice inventory contains malformed hardware UDID or connection properties")
        if hardware_id in hardware_ids or identifier in identifiers:
            raise NetworkBindingError("CoreDevice inventory contains duplicate or contradictory device records")
        if "udid" in device and device["udid"] != hardware_id:
            raise NetworkBindingError("CoreDevice inventory contains contradictory hardware UDID fields")
        hardware_ids.add(hardware_id)
        identifiers.add(identifier)
        tunnel_ip = connection.get("tunnelIPAddress")
        if tunnel_ip is not None and tunnel_ip != "":
            if not isinstance(tunnel_ip, str) or "%" in tunnel_ip:
                raise NetworkBindingError("CoreDevice returned no valid tunnel IP address")
            try:
                address = ipaddress.ip_address(tunnel_ip)
            except ValueError as exc:
                raise NetworkBindingError("CoreDevice returned no valid tunnel IP address") from exc
            if connection["tunnelState"] == "connected":
                if address in active_addresses:
                    raise NetworkBindingError("CoreDevice inventory contains duplicate active tunnel IP addresses")
                active_addresses.add(address)
        if hardware_id == udid:
            selected = device
    if selected is None:
        raise CoreDeviceUnavailableError("Selected hardware UDID is not present in the CoreDevice inventory")
    return {"result": selected}


async def _coredevice_details(udid: str, timeout: float) -> dict:
    """Read fresh inventory identity, retrying one timeout after its child is reaped.

    Each of the at most two reads keeps the supplied deadline. Total command
    waiting is at most twice that deadline, plus owned-process termination and
    reaping (up to two seconds of SIGTERM grace for each timed-out child).
    Identity validation and WDA requests occur outside this retry boundary.
    """
    try:
        return await _coredevice_details_once(udid, timeout)
    except CoreDeviceTimeoutError:
        return await _coredevice_details_once(udid, timeout)


class NetworkBinding:
    """Explicit network mode for one hardware UDID and current tunnel IP."""

    def __init__(self, udid: str, wda_url: str, *, timeout: float = 15.0):
        if not isinstance(udid, str) or not udid:
            raise ValueError("An explicit hardware UDID is required for network mode")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("CoreDevice validation timeout must be finite and positive")
        self.udid = udid
        self.origin, self.host, self.port, self._address = _parse_origin(wda_url)
        self.timeout = timeout

    @classmethod
    async def for_device(cls, udid: str, port: int = 8100) -> "NetworkBinding":
        """Resolve the paired phone's IP, bootstrapping an inactive link once."""
        if not isinstance(udid, str) or not udid:
            raise ValueError("An explicit hardware UDID is required for network mode")
        data = await _coredevice_details(udid, 15.0)
        try:
            address = cls._details_address(data, udid)
        except CoreDeviceUnavailableError:
            identifier = data["result"].get("identifier")
            if not isinstance(identifier, str) or not identifier:
                raise NetworkBindingError("CoreDevice returned no device identifier for connection bootstrap")
            # A read-only lock query can establish an inactive CoreDevice link.
            # Its boolean cannot prove unlock; only fresh inventory selects IP.
            await device_passcode_required(udid, 15.0, identifier=identifier)
            data = await _coredevice_details(udid, 15.0)
            address = cls._details_address(data, udid)
        host = f"[{address}]" if address.version == 6 else str(address)
        binding = cls(udid, f"http://{host}:{port}")
        return binding

    async def validate(self) -> "NetworkBinding":
        data = await _coredevice_details(self.udid, self.timeout)
        self._validate_details(data)
        return self

    @staticmethod
    def _details_address(data: dict, udid: str):
        result = data.get("result")
        if not isinstance(result, dict):
            raise NetworkBindingError("CoreDevice returned no device result")
        hardware = result.get("hardwareProperties")
        connection = result.get("connectionProperties")
        if not isinstance(hardware, dict) or hardware.get("udid") != udid:
            raise NetworkBindingError("CoreDevice hardware UDID does not match the selected device")
        if not isinstance(connection, dict):
            raise NetworkBindingError("CoreDevice returned no connection properties")
        if connection.get("pairingState") != "paired":
            raise NetworkBindingError("Selected device has no connected paired CoreDevice tunnel")
        if connection.get("tunnelState") in ("connecting", "disconnected"):
            raise CoreDeviceUnavailableError("Selected device has no connected paired CoreDevice tunnel")
        if connection.get("tunnelState") != "connected":
            raise NetworkBindingError("Selected device has no connected paired CoreDevice tunnel")
        tunnel_ip = connection.get("tunnelIPAddress")
        if tunnel_ip is None or tunnel_ip == "":
            raise CoreDeviceUnavailableError("Selected paired device has no tunnel IP address yet")
        if not isinstance(tunnel_ip, str) or "%" in tunnel_ip:
            raise NetworkBindingError("CoreDevice returned no valid tunnel IP address")
        try:
            return ipaddress.ip_address(tunnel_ip)
        except (ValueError, TypeError) as exc:
            raise NetworkBindingError("CoreDevice returned no valid tunnel IP address") from exc

    def _validate_details(self, data: dict) -> None:
        current_address = self._details_address(data, self.udid)
        if current_address != self._address:
            raise NetworkAddressChangedError("WDA URL IP does not match the selected device's current tunnel IP")

    def connection_factory(self, udid: str, remote_port: int) -> "BoundDirectConnection":
        if udid != self.udid:
            raise NetworkBindingError("Media connection UDID does not match the network binding")
        return BoundDirectConnection(self, remote_port)

    async def passcode_required(self) -> bool:
        """Read lock state only after fresh exact-hardware identity validation."""
        details = await _coredevice_details(self.udid, self.timeout)
        self._validate_details(details)
        identifier = details["result"].get("identifier")
        if not isinstance(identifier, str) or not identifier:
            raise NetworkBindingError("CoreDevice returned no device identifier for lock state")
        return await device_passcode_required(self.udid, self.timeout, identifier=identifier)


async def device_passcode_required(udid: str, timeout: float, *, identifier: str | None = None) -> bool:
    data = await _coredevice_query_once(udid, timeout, "lockState")
    result = data.get("result")
    value = result.get("passcodeRequired") if isinstance(result, dict) else None
    since_boot = result.get("unlockedSinceBoot") if isinstance(result, dict) else None
    actual = result.get("deviceIdentifier") if isinstance(result, dict) else None
    if (not isinstance(value, bool) or not isinstance(since_boot, bool)
            or not isinstance(actual, str) or not actual
            or (identifier is not None and (not isinstance(identifier, str) or actual != identifier))):
        raise NetworkBindingError("CoreDevice returned invalid device lock state")
    return value or not since_boot


class BoundDirectConnection(DirectConnection):
    """Revalidate the binding before opening each direct media connection."""

    def __init__(self, binding: NetworkBinding, remote_port: int):
        self.binding = binding
        super().__init__(binding.udid, remote_port, host=binding.host)

    async def open(self) -> "BoundDirectConnection":
        await self.binding.validate()
        return self


class BoundWDAClient(WDAClient):
    """Revalidate the hardware binding before each WDA HTTP request."""

    def __init__(self, binding: NetworkBinding, *, timeout: float = 45.0):
        self.binding = binding
        super().__init__(binding.origin, timeout=timeout)

    async def call(self, method: str, path: str, body=None, *, request_timeout: float | None = None):
        await self.binding.validate()
        try:
            kwargs = {} if request_timeout is None else {"request_timeout": request_timeout}
            return await super().call(method, path, body, **kwargs)
        except (aiohttp.ClientError, TimeoutError, ConnectionError) as exc:
            raise WDATransportError("WDA transport failed; the action outcome may be unknown") from exc
