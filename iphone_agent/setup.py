"""Build, verify and install one signed WDA XCTest runner for a USB iPhone."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import tempfile
import uuid
from typing import Any
from xml.etree import ElementTree


TUNNEL_BUILDER_SHA256 = "57a91a96e6a66095c1864e773a75973bd24bc06b4a05a7591f07857844daccd8"
TUNNEL_BUILD_TIMEOUT = 900.0
PROCESS_TERMINATION_TIMEOUT = 10.0


def _authentication_flags(args: Any) -> list[str]:
    """Validate the complete App Store Connect key trio before external work."""
    key_path = getattr(args, "authentication_key_path", None)
    key_id = getattr(args, "authentication_key_id", None)
    issuer_id = getattr(args, "authentication_key_issuer_id", None)
    supplied = (key_path is not None, bool(key_id), bool(issuer_id))
    if any(supplied) and not all(supplied):
        raise ValueError("Provide authentication key path, key ID and issuer ID together")
    if not all(supplied):
        return []
    path = Path(key_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Authentication key file does not exist: {path}")
    return ["-authenticationKeyPath", str(path), "-authenticationKeyID", str(key_id),
            "-authenticationKeyIssuerID", str(issuer_id)]


def _source_copy(source: Path, root: Path, *, headless: bool = False) -> Path:
    """Prepare a private build tree; the dependency checkout stays unchanged."""
    copied = root / "WebDriverAgent"
    shutil.copytree(source, copied, ignore=shutil.ignore_patterns(".git", "node_modules", "xcuserdata"))
    if not headless:
        return copied
    project = copied / "WebDriverAgent.xcodeproj" / "project.pbxproj"
    if not project.is_file():
        raise ValueError(f"WebDriverAgent Xcode project file missing: {project}")
    original = project.read_text()
    lines = original.splitlines(keepends=True)
    target = "/* Assets.xcassets in Resources */"
    matching = [line for line in lines if target in line]
    if len(matching) != 2 or not any("isa = PBXBuildFile" in line for line in matching):
        raise RuntimeError("Cannot locate exactly the runner asset catalog build and resource entries")
    project.write_text("".join(line for line in lines if target not in line))
    return copied


def _tunnel_builder(source: Path) -> Path:
    """Accept the documented builder from the supported WDA source layout."""
    source = source.resolve()
    script = source / "Scripts/build-hev-socks5-tunnel.sh"
    scheme = source / "WebDriverAgent.xcodeproj/xcshareddata/xcschemes/WebDriverAgentRunnerTunnel.xcscheme"
    try:
        package = json.loads((source / "package.json").read_text())
        targets = {entry.get("BlueprintName") for entry in ElementTree.parse(scheme).iter("BuildableReference")}
        supported = (isinstance(package, dict) and package.get("name") == "appium-webdriveragent"
                     and "WebDriverAgentTunnel" in targets)
        trusted = (script.resolve() == script and script.is_file()
                   and hashlib.sha256(script.read_bytes()).hexdigest() == TUNNEL_BUILDER_SHA256)
    except (OSError, ValueError, ElementTree.ParseError) as exc:
        raise RuntimeError("Tunnel preparation requires the supported WDA source and builder") from exc
    if not supported or not trusted:
        raise RuntimeError("Tunnel preparation requires the supported WDA source and pinned builder")
    return script


def _verify_tunnel_framework(source: Path) -> None:
    framework = (source / "ThirdParty/HevSocks5Tunnel.xcframework").resolve()
    try:
        if not framework.is_relative_to(source.resolve()):
            raise ValueError("Framework escapes private source")
        info = plistlib.loads((framework / "Info.plist").read_bytes())
        if not isinstance(info, dict) or info.get("CFBundlePackageType") != "XFWK":
            raise ValueError("Not an XCFramework")
        for entry in info.get("AvailableLibraries", []):
            architectures = entry.get("SupportedArchitectures") if isinstance(entry, dict) else None
            if (not isinstance(entry, dict) or entry.get("SupportedPlatform") != "ios"
                    or entry.get("SupportedPlatformVariant") or not isinstance(architectures, list)
                    or "arm64" not in architectures):
                continue
            identifier, library_path, headers_path = (entry.get(key) for key in
                                                      ("LibraryIdentifier", "LibraryPath", "HeadersPath"))
            if not all(isinstance(value, str) and value for value in (identifier, library_path, headers_path)):
                raise ValueError("Missing library paths")
            library = (framework / identifier / library_path).resolve()
            header = (framework / identifier / headers_path / "HevSocks5Tunnel/hev-main.h").resolve()
            if (not library.is_relative_to(framework) or not header.is_relative_to(framework)
                    or not library.read_bytes().startswith(b"!<arch>\n") or not header.read_bytes()):
                raise ValueError("Missing device archive or headers")
            return
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException) as exc:
        raise RuntimeError("Tunnel engine preparation did not produce a valid arm64 iOS framework") from exc
    raise RuntimeError("Tunnel engine framework has no arm64 iOS library")


async def _run_command(step: str, command: list[str], *, timeout: float | None = None) -> None:
    """Run one child process; cancellation terminates only its process group."""
    print(json.dumps({"step": step, "command": command[0]}), flush=True)
    process = await asyncio.create_subprocess_exec(*command, start_new_session=True)
    try:
        status = await asyncio.wait_for(process.wait(), timeout=timeout)
    except BaseException as original:
        try:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=PROCESS_TERMINATION_TIMEOUT)
                except TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(process.wait(), timeout=PROCESS_TERMINATION_TIMEOUT)
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(f"{step} and process cleanup failed", [original, cleanup_error]) from original
        raise
    if status != 0:
        raise RuntimeError(f"{step} failed with exit status {status}")


def _runner_bundle_id(app_path: Path) -> str:
    info_path = app_path / "Info.plist"
    if not info_path.is_file():
        raise RuntimeError(f"Built runner has no Info.plist: {info_path}")
    with info_path.open("rb") as stream:
        info = plistlib.load(stream)
    bundle_id = info.get("CFBundleIdentifier")
    if not isinstance(bundle_id, str) or not bundle_id:
        raise RuntimeError(f"Built runner has no CFBundleIdentifier: {info_path}")
    return bundle_id


def _write_runner_metadata(home: Path, payload: dict[str, str]) -> None:
    """Replace runner.json atomically only after the exact built app is installed."""
    path = home / "runner.json"
    fd, temporary = tempfile.mkstemp(prefix=".runner-", suffix=".json", dir=home)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(home, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def build_runner(args: Any) -> int:
    """Build a signed physical-device runner, verify it, and install that app."""
    from .device import DeviceServices

    auth_flags = _authentication_flags(args)
    source = Path(args.source).expanduser().resolve()
    original_project = source / "WebDriverAgent.xcodeproj"
    if not original_project.is_dir():
        raise ValueError(f"WebDriverAgent source project missing: {original_project}")
    if args.tunnel and not (source / "ThirdParty/hev-socks5-tunnel/src/hev-main.h").is_file():
        raise RuntimeError("Initialize the WDA tunnel engine with git submodule update --init --recursive in the WDA source")
    if args.tunnel:
        _tunnel_builder(source)

    paired_network = bool(getattr(args, "paired_network", False))
    if paired_network and not args.udid:
        raise ValueError("Paired-network setup requires an explicit --udid")
    devices = [] if paired_network else await DeviceServices.discover()
    udid = args.udid
    if not udid:
        if len(devices) != 1:
            raise RuntimeError("Connect one USB iPhone or select --udid")
        udid = devices[0]["udid"]
    if paired_network:
        from .network import NetworkBinding
        await NetworkBinding.for_device(udid)
    else:
        device = DeviceServices(udid)
        try:
            await device.info()  # USB pairing and UDID preflight before a costly build.
        finally:
            await device.close()

    home = Path(args.home).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Unique derived data avoids stale provisioning-profile and product caches.
    derived = home / "build" / uuid.uuid4().hex
    scheme = "WebDriverAgentRunnerTunnel" if args.tunnel else "WebDriverAgentRunner"
    headless = bool(getattr(args, "headless_assets", False))
    build_only = bool(getattr(args, "build_only", False))

    async def build(build_source: Path) -> None:
        project = build_source / "WebDriverAgent.xcodeproj"
        command = [
            "xcodebuild", "build-for-testing", "-project", str(project),
            "-scheme", scheme, "-derivedDataPath", str(derived), "-allowProvisioningUpdates",
            *auth_flags, f"DEVELOPMENT_TEAM={args.team}",
            f"WDA_PRODUCT_BUNDLE_IDENTIFIER={args.bundle_id}", "CODE_SIGN_STYLE=Automatic",
        ]
        if headless:
            command.extend(["-sdk", "iphoneos", "-arch", "arm64", "SUPPORTED_PLATFORMS=iphoneos",
                            "SUPPORTS_MACCATALYST=NO", "RUN_CLANG_STATIC_ANALYZER=NO",
                            "ASSETCATALOG_COMPILER_APPICON_NAME="])
        else:
            command.extend(["-destination", f"platform=iOS,id={udid}"])
        if args.tunnel:
            command.extend([
                f"WDA_RUNNER_ENTITLEMENTS={build_source / 'WebDriverAgentRunner/WebDriverAgentRunner.entitlements'}",
                f"WDA_TUNNEL_ENTITLEMENTS={build_source / 'WebDriverAgentTunnel/WebDriverAgentTunnel.entitlements'}",
            ])
        await _run_command("build-for-testing", command)

    with tempfile.TemporaryDirectory(prefix="iphone-agent-wda-") as temporary:
        copied = _source_copy(source, Path(temporary), headless=headless)
        patch = Path(__file__).with_name("wda_local.patch")
        await _run_command("apply-runner-patch", ["patch", "--batch", "--forward", "-p1",
                                                "-d", str(copied), "-i", str(patch)])
        if args.tunnel:
            script = _tunnel_builder(copied)
            await _run_command("prepare-tunnel-engine", ["/bin/bash", str(script)], timeout=TUNNEL_BUILD_TIMEOUT)
            _verify_tunnel_framework(copied)
        await build(copied)

    app_path = derived / "Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app"
    if not app_path.is_dir():
        raise RuntimeError(f"Signed runner app was not built: {app_path}")
    expected_id = f"{args.bundle_id}.xctrunner"
    actual_id = _runner_bundle_id(app_path)
    if actual_id != expected_id:
        raise RuntimeError(f"Built runner bundle ID {actual_id!r} does not match {expected_id!r}")
    await _run_command("verify-signature", ["codesign", "--verify", "--deep", "--strict", str(app_path)])

    if build_only:
        print(json.dumps({"step": "build-complete", "appPath": str(app_path), "bundleId": actual_id}), flush=True)
        return 0

    if paired_network:
        await NetworkBinding.for_device(udid)
        await _run_command("install-runner", ["xcrun", "devicectl", "device", "install", "app",
                                               "--device", udid, str(app_path), "--quiet"])
    else:
        installer = DeviceServices(udid)
        try:
            await installer.install(str(app_path))
        finally:
            await installer.close()
    from .housekeeping import prune_builds
    try:
        previous = [json.loads((home / "runner.json").read_text())["appPath"]]
    except (OSError, ValueError, KeyError, TypeError):
        previous = []
    _write_runner_metadata(home, {"udid": udid, "bundleId": actual_id, "appPath": str(app_path)})
    print(json.dumps({"step": "installed", "appPath": str(app_path), "bundleId": actual_id,
                      "metadata": str(home / "runner.json")}), flush=True)
    # A session started before this setup may still run the previous build; the next controller start removes it.
    try:
        removed = prune_builds(home, keep=previous)
    except Exception as exc:  # The runner is installed; tidying is not part of that outcome.
        print(json.dumps({"step": "old-builds-kept", "error": str(exc)}), flush=True)
        removed = []
    if removed:
        print(json.dumps({"step": "removed-old-builds", "count": len(removed),
                          "bytes": sum(size for _, size in removed)}), flush=True)
    return 0
