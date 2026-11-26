"""USB-only iPhone services backed by pymobiledevice3 11.47.0.

AFC paths are relative to the device's Media directory. When ``bundle_id`` is
provided, House Arrest instead vends that application's container. Neither
namespace exposes the host file system.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pymobiledevice3.exceptions import AfcFileNotFoundError
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.services.house_arrest import HouseArrestService
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.usbmux import list_devices

from .audit import json_safe


class DeviceServices:
    """Operations for one explicitly selected, paired USB iPhone."""

    def __init__(self, udid: str):
        if not isinstance(udid, str) or not udid.strip():
            raise ValueError("An explicit device UDID is required")
        self.udid = udid.strip()
        self._lockdown: Any | None = None
        self._connection_lock = asyncio.Lock()

    @staticmethod
    async def discover() -> list[dict[str, Any]]:
        """List USB devices visible to usbmuxd without connecting or pairing."""
        devices = await list_devices()
        return [
            {"udid": device.serial, "connection_type": device.connection_type, "device_id": device.devid}
            for device in devices
            if device.connection_type == "USB"
        ]

    async def usb_present(self) -> bool:
        """Report current USB presence without pairing or starting a service."""
        return any(item["udid"] == self.udid for item in await self.discover())

    async def _client(self) -> Any:
        async with self._connection_lock:
            return await self._connect_locked()

    async def _connect_locked(self) -> Any:
        if self._lockdown is not None:
            return self._lockdown
        try:
            devices = await list_devices()
            matches = [
                device for device in devices
                if device.connection_type == "USB" and device.matches_udid(self.udid)
            ]
            if not matches:
                raise RuntimeError(f"USB device {self.udid} is not connected")
            if len(matches) != 1:
                raise RuntimeError(f"USB device {self.udid} is ambiguous ({len(matches)} matches)")
            # No implicit pairing, network fallback, or first-device selection.
            self._lockdown = await create_using_usbmux(
                serial=matches[0].serial,
                connection_type="USB",
                autopair=False,
                label="Local iPhone Controller",
            )
            if not self._lockdown.paired:
                await self._lockdown.close()
                self._lockdown = None
                raise RuntimeError(f"USB device {self.udid} is not paired with this host")
            return self._lockdown
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Cannot connect to USB device {self.udid}: {exc}") from exc

    async def info(self) -> dict[str, Any]:
        """Return the device's current lockdown values."""
        try:
            values = await (await self._client()).get_value()
            if not isinstance(values, dict):
                raise RuntimeError("Device returned no information")
            return json_safe(values)
        except Exception as exc:
            await self._fail("read device information", exc)

    async def packages(self) -> list[dict[str, Any]]:
        """Browse installed applications through installation_proxy."""
        try:
            async with InstallationProxyService(await self._client()) as service:
                return json_safe(await service.browse())
        except Exception as exc:
            await self._fail("list installed applications", exc)

    async def install(self, path: str) -> None:
        """Install the caller-selected local IPA or application package."""
        package = Path(path)
        if not package.exists():
            raise RuntimeError(f"Install package does not exist: {package}")
        try:
            async with InstallationProxyService(await self._client()) as service:
                await service.install_from_local(package)
        except Exception as exc:
            await self._fail(f"install {package}", exc)

    async def uninstall(self, bundle_id: str) -> None:
        bundle_id = self._bundle_id(bundle_id)
        try:
            async with InstallationProxyService(await self._client()) as service:
                await service.uninstall(bundle_id)
        except Exception as exc:
            await self._fail(f"uninstall {bundle_id}", exc)

    async def erase(self) -> dict[str, str]:
        """Request an erase; the controller enforces its explicit erase gate."""
        from pymobiledevice3.services.mobile_config import MobileConfigService
        try:
            async with MobileConfigService(await self._client()) as service:
                await service.erase_device(preserve_data_plan=True, disallow_proximity_setup=False)
            await self.close()
            return {"status": "requested", "effect": "Verify device state after reconnect"}
        except Exception as exc:
            await self._fail("request device erase", exc)

    @staticmethod
    def _bundle_id(bundle_id: str) -> str:
        if not isinstance(bundle_id, str) or not bundle_id.strip() or "/" in bundle_id or "\x00" in bundle_id:
            raise ValueError("A valid application bundle ID is required")
        return bundle_id.strip()

    @staticmethod
    def _device_path(path: str) -> str:
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or "\\" in path:
            raise ValueError("Device path must be an absolute slash-separated path")
        components = path.split("/")[1:]
        if any(component in (".", "..") for component in components):
            raise ValueError("Device path may not contain . or .. components")
        return "/" + "/".join(component for component in components if component)

    @asynccontextmanager
    async def _files(self, bundle_id: str | None) -> AsyncIterator[AfcService]:
        client = await self._client()
        if bundle_id is None:
            async with AfcService(client) as service:
                yield service
        else:
            bundle_id = self._bundle_id(bundle_id)
            async with await HouseArrestService.create(client, bundle_id) as service:
                yield service

    @staticmethod
    async def _check_no_symlink(service: AfcService, path: str, *, final_may_be_missing: bool = False) -> None:
        """Reject symbolic links in every existing component before AFC access."""
        if path == "/":
            return
        current = ""
        parts = path.strip("/").split("/")
        for index, component in enumerate(parts):
            current += "/" + component
            try:
                attrs = await service.stat(current)
            except AfcFileNotFoundError:
                if final_may_be_missing and index == len(parts) - 1:
                    return
                raise
            if attrs.get("st_ifmt") == "S_IFLNK" or "LinkTarget" in attrs:
                raise RuntimeError(f"Symbolic link is not allowed in device path: {current}")

    async def files_list(self, path: str, bundle_id: str | None = None) -> list[dict[str, Any]]:
        path = self._device_path(path)
        try:
            async with self._files(bundle_id) as service:
                await self._check_no_symlink(service, path)
                names = await service.listdir(path)
                entries = []
                for name in names:
                    child = path.rstrip("/") + "/" + name
                    # Do not follow a symlink to stat its target.
                    attrs = await service.stat(child)
                    entries.append({"name": name, "path": child, "type": attrs.get("st_ifmt"),
                                    "size": attrs.get("st_size"), "modified": attrs.get("st_mtime")})
                return json_safe(entries)
        except Exception as exc:
            await self._fail(f"list device files at {path}", exc)

    async def files_read(self, path: str, bundle_id: str | None = None) -> bytes:
        path = self._device_path(path)
        try:
            async with self._files(bundle_id) as service:
                await self._check_no_symlink(service, path)
                return await service.get_file_contents(path)
        except Exception as exc:
            await self._fail(f"read device file {path}", exc)

    async def files_write(self, path: str, data: bytes, bundle_id: str | None = None) -> None:
        path = self._device_path(path)
        if path == "/":
            raise ValueError("Cannot write to the device file root")
        if not isinstance(data, bytes):
            raise TypeError("Device file data must be bytes")
        try:
            async with self._files(bundle_id) as service:
                await self._check_no_symlink(service, path, final_may_be_missing=True)
                await service.set_file_contents(path, data)
        except Exception as exc:
            await self._fail(f"write device file {path}", exc)

    async def files_delete(self, path: str, bundle_id: str | None = None) -> None:
        path = self._device_path(path)
        if path == "/":
            raise ValueError("Cannot delete the device file root")
        try:
            async with self._files(bundle_id) as service:
                await self._check_no_symlink(service, path)
                await service.rm_single(path)
        except Exception as exc:
            await self._fail(f"delete device file {path}", exc)

    async def timezone_get(self) -> str:
        try:
            value = await (await self._client()).get_value(key="TimeZone")
            if not isinstance(value, str):
                raise RuntimeError("Device did not return a time zone")
            return value
        except Exception as exc:
            await self._fail("read device time zone", exc)

    async def timezone_set(self, timezone: str) -> None:
        if not isinstance(timezone, str):
            raise ValueError("Time zone must be an IANA identifier")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ValueError(f"Unknown IANA time zone: {timezone}") from exc
        try:
            await (await self._client()).set_timezone(timezone)
        except Exception as exc:
            await self._fail(f"set device time zone to {timezone}", exc)

    async def time(self) -> int:
        """Read the device's Unix time in whole seconds."""
        try:
            value = await (await self._client()).get_value(key="TimeIntervalSince1970")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RuntimeError("Device did not return a Unix timestamp")
            return int(value)
        except Exception as exc:
            await self._fail("read device time", exc)

    async def close(self) -> None:
        async with self._connection_lock:
            client, self._lockdown = self._lockdown, None
            if client is not None:
                await client.close()

    async def _fail(self, operation: str, exc: Exception) -> None:
        # Never replay a failed mutation. Drop the cached transport so the next
        # request revalidates the exact USB device and pairing.
        try:
            await self.close()
        except Exception:
            pass
        if isinstance(exc, RuntimeError) and str(exc).startswith("USB device"):
            raise exc
        raise RuntimeError(f"Cannot {operation}: {exc}") from exc
