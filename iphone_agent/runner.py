"""Own or explicitly adopt one WebDriverAgent XCTest runner on a USB iPhone.

The rootless launch path uses pymobiledevice3's in-process CoreDeviceProxy
tunnel and XCUITest testmanager service. It does not require an Xcode simulator
runtime. The runner app must already be installed and signed for the device.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import aiohttp
from pymobiledevice3.lockdown import create_using_usbmux
import pymobiledevice3.remote.userspace_tunnel as userspace_tunnel
from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane, UserspaceRsdTunnel
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.tunnel_service import CoreDeviceTunnelProxy
from pymobiledevice3.services.dvt.testmanaged.xcuitest import TestConfig, XCUITestService
from pymobiledevice3.usbmux import list_devices

from .transport import USBForward
from .wda import WDAClient, WDAError


class RunnerError(RuntimeError):
    pass


class USBUserspaceRsdTunnel(UserspaceRsdTunnel):
    """USB-only setup adapter for pinned pymobiledevice3 11.47.0.

    The upstream constructor has no provider or connection-type injection. Keep
    its aopen/aclose lifecycle lock, singleton markers, exit stack and transport
    watcher, replacing only provider selection with an explicit USB connection.
    No module factory is replaced, and RemotePairing discovery is never used.
    """

    async def _aopen_locked(self):
        if userspace_tunnel._active_tunnel is not None:
            raise RunnerError("a userspace tunnel is already active in this process")
        if not self.serial or self.autopair or self.remotepairing_fallback:
            raise RunnerError("USB runner tunnel requires an explicit UDID and pairing/discovery disabled")
        userspace_tunnel.tunnel_service.USE_USERSPACE_TUNNEL = True
        stack = AsyncExitStack()
        try:
            lockdown = await create_using_usbmux(
                serial=self.serial, connection_type="USB", autopair=False,
                label="Local iPhone XCTest Runner",
            )
            stack.push_async_callback(lockdown.close)
            if lockdown.udid != self.serial:
                raise RunnerError(f"USB lockdown returned {lockdown.udid!r}, expected {self.serial!r}")
            if not lockdown.paired:
                raise RunnerError("USB runner tunnel requires an already paired iPhone")
            provider = await CoreDeviceTunnelProxy.create(lockdown)
            stack.push_async_callback(provider.close)
            result = await stack.enter_async_context(provider.start_tcp_tunnel())
            self.tun = result.client.tun
            self.tun.set_peer(result.address)
            dial_plane = await stack.enter_async_context(UserspaceDialPlane(self.tun, result.address))
            rsd = RemoteServiceDiscoveryService(
                (result.address, result.port), open_connection=dial_plane.dial,
                auxiliary_metadata=result.auxiliary_metadata,
            )
            stack.push_async_callback(rsd.close)
            await rsd.connect()
        except BaseException:
            try:
                await stack.aclose()
            finally:
                userspace_tunnel.tunnel_service.USE_USERSPACE_TUNNEL = False
                self.tun = None
            raise
        self._exit_stack = stack
        self.rsd = rsd
        userspace_tunnel._active_tunnel = self
        userspace_tunnel.USERSPACE_ACTIVE = True
        self._transport_watcher = asyncio.create_task(
            self._watch_transport_closed(result.client),
            name=f"usb-runner-tunnel-watcher-{result.address}",
        )
        return rsd


class WDARunner:
    """Keep a UDID-bound WDA test plan and USB HTTP forward alive until close."""

    def __init__(self, udid: str, bundle_id: str, port: int = 8100):
        if not udid:
            raise ValueError("an explicit device UDID is required")
        if not bundle_id or not bundle_id.endswith(".xctrunner"):
            raise ValueError("bundle_id must be the installed .xctrunner bundle identifier")
        if not 1 <= port <= 65535:
            raise ValueError("WDA port must be 1..65535")
        self.udid = udid
        self.bundle_id = bundle_id
        self.port = port
        self.expected_product_bundle_id = bundle_id.removesuffix(".xctrunner")
        self.forward: USBForward | None = None
        self.wda: WDAClient | None = None
        self.tunnel: USBUserspaceRsdTunnel | None = None
        self.task: asyncio.Task[None] | None = None
        self.status: dict[str, Any] | None = None
        self.adopted = False
        self._open = False
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> WDARunner:
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _usb_preflight(self) -> None:
        devices = await list_devices()
        matches = [device for device in devices if device.connection_type == "USB" and device.matches_udid(self.udid)]
        if len(matches) != 1:
            raise RunnerError(f"expected exactly one USB iPhone matching {self.udid}; found {len(matches)}")
        lockdown = await create_using_usbmux(
            serial=matches[0].serial, connection_type="USB", autopair=False,
            label="Local iPhone XCTest Runner",
        )
        try:
            if not lockdown.paired:
                raise RunnerError(f"USB iPhone {self.udid} is not paired with this Mac")
        finally:
            await lockdown.close()

    def _validate_status(self, status: Any) -> dict[str, Any]:
        if not isinstance(status, dict) or status.get("ready") is not True or status.get("state") != "success":
            raise RunnerError("port answers HTTP but WebDriverAgent is not ready")
        build = status.get("build")
        product_id = build.get("productBundleIdentifier") if isinstance(build, dict) else None
        if product_id != self.expected_product_bundle_id:
            raise RunnerError(
                f"port serves WebDriverAgent bundle {product_id!r}, expected {self.expected_product_bundle_id!r}"
            )
        return status

    async def _probe(self) -> dict[str, Any] | None:
        assert self.wda is not None
        try:
            return self._validate_status(await self.wda.call("GET", "/status"))
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
            return None
        except WDAError as exc:
            raise RunnerError(f"port {self.port} serves an unexpected HTTP response: {exc.code}") from exc

    async def open(self, *, timeout: float = 45.0) -> WDARunner:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        async with self._lock:
            if self._open:
                if self.task is not None and self.task.done():
                    self.task.result()
                    raise RunnerError("WebDriverAgent test runner stopped unexpectedly")
                return self
            deadline = asyncio.get_running_loop().time() + timeout
            startup = asyncio.timeout_at(deadline)
            try:
                async with startup:
                    await self._usb_preflight()
                    self.forward = USBForward(self.udid, self.port)
                    await self.forward.open()
                    self.wda = WDAClient(f"http://127.0.0.1:{self.forward.port}", timeout=2.0)
                    existing = await self._probe()
                    if existing is not None:
                        # Same bundle identity, same selected USB phone. Never stop
                        # or relaunch a runner that another process already owns.
                        self.status = existing
                        self.adopted = True
                        self._open = True
                        return self

                    self.tunnel = USBUserspaceRsdTunnel(
                        serial=self.udid, autopair=False, remotepairing_fallback=False
                    )
                    rsd = await self.tunnel.aopen()
                    if rsd.udid != self.udid:
                        raise RunnerError(f"RSD handshake returned {rsd.udid!r}, expected {self.udid!r}")
                    config = await TestConfig.create_for(rsd, runner_bundle_id=self.bundle_id)
                    config.runner_app_env = {
                        "WDA_PRODUCT_BUNDLE_IDENTIFIER": self.expected_product_bundle_id,
                        "USE_PORT": str(self.port),
                        "USE_IP": "127.0.0.1",
                    }
                    self.task = asyncio.create_task(XCUITestService(rsd).run(config), name=f"wda-xctest-{self.udid}")

                    while asyncio.get_running_loop().time() < deadline:
                        if self.task.done():
                            self.task.result()
                            raise RunnerError("WebDriverAgent test runner exited before HTTP readiness")
                        status = await self._probe()
                        if status is not None:
                            # Let a just-started test plan report an immediate
                            # failure before declaring the HTTP response healthy.
                            await asyncio.sleep(0)
                            if self.task.done():
                                self.task.result()
                                raise RunnerError("WebDriverAgent test runner exited during readiness")
                            self.status = status
                            self._open = True
                            return self
                        await asyncio.sleep(0.2)
                    raise TimeoutError(f"WebDriverAgent did not become ready on USB port {self.port} within {timeout:g} seconds")
            except BaseException as exc:
                await self._cleanup(suppress_errors=True)
                if isinstance(exc, TimeoutError) and startup.expired():
                    raise TimeoutError(
                        f"WebDriverAgent did not become ready on USB port {self.port} within {timeout:g} seconds"
                    ) from exc
                raise

    async def _cleanup(self, *, suppress_errors: bool = False) -> None:
        self._open = False
        self.status = None
        self.adopted = False
        errors: list[Exception] = []
        if self.task is not None:
            task, self.task = self.task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.tunnel is not None:
            tunnel, self.tunnel = self.tunnel, None
            try:
                await tunnel.aclose()
            except Exception as exc:
                errors.append(exc)
        if self.wda is not None:
            wda, self.wda = self.wda, None
            try:
                await wda.close()
            except Exception as exc:
                errors.append(exc)
        if self.forward is not None:
            forward, self.forward = self.forward, None
            try:
                await forward.close()
            except Exception as exc:
                errors.append(exc)
        if errors and not suppress_errors:
            raise RunnerError(f"WebDriverAgent cleanup failed: {errors[0]}") from errors[0]

    async def close(self) -> None:
        async with self._lock:
            await self._cleanup()
