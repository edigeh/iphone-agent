"""Setup must install only a verified, exact Xcode runner product."""

import asyncio
import hashlib
import json
from pathlib import Path
import plistlib
import signal
from types import SimpleNamespace

import pytest

from iphone_agent import setup
from iphone_agent import device as device_module


class FakeDevice:
    discovered = [{"udid": "USB-1", "connection_type": "USB", "device_id": 1}]
    installs = []
    calls = []
    install_error = None

    def __init__(self, udid):
        assert udid == "USB-1"
        self.udid = udid

    @classmethod
    async def discover(cls):
        cls.calls.append("discover")
        return cls.discovered

    async def info(self):
        type(self).calls.append("info")
        return {"DeviceName": "iPhone"}

    async def install(self, path):
        type(self).calls.append("install")
        if type(self).install_error:
            raise type(self).install_error
        type(self).installs.append(path)

    async def close(self):
        type(self).calls.append("close")


@pytest.fixture
def project(tmp_path, monkeypatch):
    source = tmp_path / "source"
    pbx = source / "WebDriverAgent.xcodeproj/project.pbxproj"
    pbx.parent.mkdir(parents=True)
    pbx.write_text("""\
A /* Assets.xcassets in Resources */ = {isa = PBXBuildFile; fileRef = B; };
B /* Assets.xcassets */ = {isa = PBXFileReference; path = WebDriverAgentRunner/Assets.xcassets; };
files = ( A /* Assets.xcassets in Resources */, );
keep = ( source code );
""")
    (source / "WebDriverAgentRunner/Assets.xcassets").mkdir(parents=True)
    (source / ".git").mkdir()
    (source / "node_modules").mkdir()
    (source / "xcuserdata").mkdir()
    FakeDevice.installs = []
    FakeDevice.calls = []
    FakeDevice.install_error = None
    monkeypatch.setattr(device_module, "DeviceServices", FakeDevice)
    args = SimpleNamespace(source=source, home=tmp_path / "home", team="TEAM123", bundle_id="org.example.WDA",
                           udid="USB-1", tunnel=False, headless_assets=False, build_only=False,
                           authentication_key_path=None, authentication_key_id=None,
                           authentication_key_issuer_id=None)
    return args, pbx


def product(command, bundle_id="org.example.WDA.xctrunner"):
    derived = Path(command[command.index("-derivedDataPath") + 1])
    app = derived / "Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app"
    app.mkdir(parents=True)
    with (app / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, stream)
    return app


@pytest.mark.asyncio
async def test_headless_build_copy_is_private_and_installs_exact_verified_product(project, monkeypatch):
    args, pbx = project
    args.headless_assets = True
    args.authentication_key_path = args.home.parent / "AuthKey.p8"
    args.authentication_key_path.write_text("test-key")
    args.authentication_key_id = "KEY123"
    args.authentication_key_issuer_id = "ISSUER123"
    before = pbx.read_bytes()
    seen = []

    async def fake_run(step, command):
        seen.append((step, command))
        if step == "apply-runner-patch":
            assert command[:4] == ["patch", "--batch", "--forward", "-p1"]
            assert Path(command[command.index("-d") + 1]) != args.source
            assert Path(command[-1]).name == "wda_local.patch"
        if step == "build-for-testing":
            project_path = Path(command[command.index("-project") + 1])
            copied_source = project_path.parent
            assert copied_source != args.source
            text = (project_path / "project.pbxproj").read_text()
            assert "Assets.xcassets in Resources" not in text
            assert "keep = ( source code );" in text
            assert not (copied_source / ".git").exists()
            assert not (copied_source / "node_modules").exists()
            assert not (copied_source / "xcuserdata").exists()
            assert "-sdk" in command and command[command.index("-sdk") + 1] == "iphoneos"
            assert "-arch" in command and command[command.index("-arch") + 1] == "arm64"
            assert "-destination" not in command
            assert "SUPPORTED_PLATFORMS=iphoneos" in command
            assert "ASSETCATALOG_COMPILER_APPICON_NAME=" in command
            assert "-authenticationKeyPath" in command
            assert "-authenticationKeyID" in command
            assert "-authenticationKeyIssuerID" in command
            product(command)
        elif step == "verify-signature":
            assert command[:4] == ["codesign", "--verify", "--deep", "--strict"]
            assert Path(command[-1]).is_dir()

    monkeypatch.setattr(setup, "_run_command", fake_run)
    assert await setup.build_runner(args) == 0
    assert [step for step, _ in seen] == ["apply-runner-patch", "build-for-testing", "verify-signature"]
    assert pbx.read_bytes() == before
    assert FakeDevice.installs == [str(Path(seen[2][1][-1]))]
    metadata = json.loads((args.home / "runner.json").read_text())
    assert metadata == {"udid": "USB-1", "bundleId": "org.example.WDA.xctrunner",
                        "appPath": FakeDevice.installs[0]}


@pytest.mark.asyncio
async def test_build_failure_never_installs_or_writes_metadata(project, monkeypatch):
    args, _ = project

    async def fail_build(step, command):
        raise RuntimeError("xcodebuild failed")

    monkeypatch.setattr(setup, "_run_command", fail_build)
    with pytest.raises(RuntimeError, match="xcodebuild failed"):
        await setup.build_runner(args)
    assert FakeDevice.installs == []
    assert not (args.home / "runner.json").exists()


@pytest.mark.asyncio
async def test_wrong_product_or_codesign_failure_never_installs(project, monkeypatch):
    args, _ = project

    async def wrong_product(step, command):
        if step == "build-for-testing":
            product(command, "org.other.Runner")

    monkeypatch.setattr(setup, "_run_command", wrong_product)
    with pytest.raises(RuntimeError, match="does not match"):
        await setup.build_runner(args)
    assert FakeDevice.installs == []
    assert not (args.home / "runner.json").exists()

    async def unsigned(step, command):
        if step == "build-for-testing":
            product(command)
        elif step == "verify-signature":
            raise RuntimeError("bad signature")

    monkeypatch.setattr(setup, "_run_command", unsigned)
    with pytest.raises(RuntimeError, match="bad signature"):
        await setup.build_runner(args)
    assert FakeDevice.installs == []
    assert not (args.home / "runner.json").exists()


@pytest.mark.asyncio
async def test_build_only_verifies_but_does_not_install(project, monkeypatch):
    args, _ = project
    args.build_only = True
    steps = []

    async def fake_run(step, command):
        steps.append(step)
        if step == "build-for-testing":
            assert command[command.index("-destination") + 1] == "platform=iOS,id=USB-1"
            product(command)

    monkeypatch.setattr(setup, "_run_command", fake_run)
    assert await setup.build_runner(args) == 0
    assert steps == ["apply-runner-patch", "build-for-testing", "verify-signature"]
    assert FakeDevice.installs == []
    assert not (args.home / "runner.json").exists()


@pytest.mark.asyncio
async def test_partial_authentication_key_is_rejected_before_device_or_build(project, monkeypatch):
    args, _ = project
    args.authentication_key_id = "KEY123"
    async def unexpected(*_):
        raise AssertionError("external command attempted")
    monkeypatch.setattr(setup, "_run_command", unexpected)
    with pytest.raises(ValueError, match="together"):
        await setup.build_runner(args)
    assert FakeDevice.calls == []


async def test_paired_network_setup_checks_identity_and_installs_verified_app(project, monkeypatch):
    from iphone_agent.network import NetworkBinding
    from unittest.mock import AsyncMock
    args, _ = project
    args.paired_network = True
    validate = AsyncMock()
    monkeypatch.setattr(NetworkBinding, "for_device", validate)
    steps = []

    async def run(step, command):
        steps.append((step, command))
        if step == "build-for-testing":
            product(command)
        if step == "install-runner":
            assert command[:5] == ["xcrun", "devicectl", "device", "install", "app"]
            assert command[command.index("--device") + 1] == "USB-1"
            assert Path(command[-2]).name == "WebDriverAgentRunner-Runner.app"

    monkeypatch.setattr(setup, "_run_command", run)
    assert await setup.build_runner(args) == 0
    assert [s for s, _ in steps] == ["apply-runner-patch", "build-for-testing", "verify-signature", "install-runner"]
    assert validate.await_count == 2
    assert FakeDevice.calls == []
    assert (args.home / "runner.json").is_file()


@pytest.fixture
def tunnel_project(project, monkeypatch):
    args, pbx = project
    args.tunnel = args.headless_assets = args.build_only = True
    (args.source / "package.json").write_text(json.dumps({"name": "appium-webdriveragent"}))
    scheme = args.source / "WebDriverAgent.xcodeproj/xcshareddata/xcschemes/WebDriverAgentRunnerTunnel.xcscheme"
    scheme.parent.mkdir(parents=True)
    scheme.write_text('<Scheme><BuildAction><BuildActionEntries><BuildActionEntry>'
                      '<BuildableReference BlueprintName="WebDriverAgentTunnel" />'
                      '</BuildActionEntry></BuildActionEntries></BuildAction></Scheme>')
    header = args.source / "ThirdParty/hev-socks5-tunnel/src/hev-main.h"
    header.parent.mkdir(parents=True)
    header.write_text("void hev_socks5_tunnel_main(void);\n")
    builder = args.source / "Scripts/build-hev-socks5-tunnel.sh"
    builder.parent.mkdir()
    builder.write_text("#!/bin/bash\nexit 0\n")
    monkeypatch.setattr(setup, "TUNNEL_BUILDER_SHA256", hashlib.sha256(builder.read_bytes()).hexdigest(),
                        raising=False)
    return args, pbx, builder


def tunnel_framework(source):
    framework = source / "ThirdParty/HevSocks5Tunnel.xcframework"
    library = framework / "ios-arm64"
    (library / "Headers/HevSocks5Tunnel").mkdir(parents=True)
    (library / "libhev-socks5-tunnel.a").write_bytes(b"!<arch>\n")
    (library / "Headers/HevSocks5Tunnel/hev-main.h").write_text("header")
    with (framework / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundlePackageType": "XFWK", "AvailableLibraries": [{
            "LibraryIdentifier": "ios-arm64", "LibraryPath": "libhev-socks5-tunnel.a",
            "HeadersPath": "Headers", "SupportedPlatform": "ios", "SupportedArchitectures": ["arm64"],
        }]}, stream)
    return framework


async def test_tunnel_build_prepares_missing_framework_only_in_private_copy(tunnel_project, monkeypatch):
    args, pbx, _ = tunnel_project
    before = pbx.read_bytes()
    steps = []

    async def run(step, command, **kwargs):
        steps.append(step)
        if step == "prepare-tunnel-engine":
            assert command[0] == "/bin/bash"
            source = Path(command[1]).parent.parent
            assert source != args.source
            assert kwargs["timeout"] > 0
            tunnel_framework(source)
        if step == "build-for-testing":
            source = Path(command[command.index("-project") + 1]).parent
            assert (source / "ThirdParty/HevSocks5Tunnel.xcframework/Info.plist").is_file()
            assert command[command.index("-scheme") + 1] == "WebDriverAgentRunnerTunnel"
            product(command)

    monkeypatch.setattr(setup, "_run_command", run)
    assert await setup.build_runner(args) == 0
    assert steps == ["apply-runner-patch", "prepare-tunnel-engine", "build-for-testing", "verify-signature"]
    assert pbx.read_bytes() == before
    assert not (args.source / "ThirdParty/HevSocks5Tunnel.xcframework").exists()
    assert FakeDevice.installs == [] and not (args.home / "runner.json").exists()


@pytest.mark.parametrize("failure", ["command", "invalid output", "missing archive", "simulator only", "escaped archive"])
async def test_tunnel_preparation_failure_preserves_metadata_and_never_builds(tunnel_project, monkeypatch, failure):
    args, _, _ = tunnel_project
    args.home.mkdir()
    metadata = args.home / "runner.json"
    metadata.write_text("existing runner\n")
    steps = []

    async def run(step, command, **kwargs):
        steps.append(step)
        if step == "prepare-tunnel-engine" and failure == "command":
            raise RuntimeError("engine preparation failed")
        if step == "prepare-tunnel-engine" and failure not in ("command", "invalid output"):
            framework = tunnel_framework(Path(command[1]).parent.parent)
            archive = framework / "ios-arm64/libhev-socks5-tunnel.a"
            if failure == "missing archive":
                archive.unlink()
            elif failure == "escaped archive":
                outside = args.home / "outside.a"
                archive.rename(outside)
                archive.symlink_to(outside)
            else:
                info = plistlib.loads((framework / "Info.plist").read_bytes())
                info["AvailableLibraries"][0]["SupportedPlatformVariant"] = "simulator"
                (framework / "Info.plist").write_bytes(plistlib.dumps(info))
        if step == "build-for-testing":
            product(command)

    monkeypatch.setattr(setup, "_run_command", run)
    with pytest.raises(RuntimeError, match="engine|framework"):
        await setup.build_runner(args)
    assert steps == ["apply-runner-patch", "prepare-tunnel-engine"]
    assert metadata.read_text() == "existing runner\n" and FakeDevice.installs == []


@pytest.mark.parametrize("invalid", ["script content", "script path", "source identity"])
async def test_tunnel_source_rejected_before_any_external_work(tunnel_project, monkeypatch, invalid):
    args, _, builder = tunnel_project
    if invalid == "script content":
        builder.write_text("#!/bin/bash\necho unexpected\n")
    elif invalid == "script path":
        outside = args.home.parent / "outside.sh"
        builder.rename(outside)
        builder.symlink_to(outside)
    else:
        (args.source / "package.json").write_text('{"name":"unrelated-project"}')

    async def unexpected(*args, **kwargs):
        raise AssertionError("external command attempted")

    monkeypatch.setattr(setup, "_run_command", unexpected)
    with pytest.raises(RuntimeError, match="supported|builder|source"):
        await setup.build_runner(args)
    assert FakeDevice.calls == []


@pytest.mark.parametrize("failure", ["timeout", "cancel"])
@pytest.mark.parametrize("resists_term", [False, True])
async def test_owned_command_timeout_or_cancel_reaps_its_process_group(monkeypatch, failure, resists_term):
    entered = asyncio.Event()
    signals = []

    class Process:
        pid = 43210
        returncode = None
        waits = 0

        async def wait(self):
            self.waits += 1
            entered.set()
            if self.waits == 1 or (self.waits == 2 and resists_term):
                await asyncio.Event().wait()
            self.returncode = 0
            return 0

    process = Process()

    async def create(*args, **kwargs):
        assert kwargs["start_new_session"]
        return process

    monkeypatch.setattr(setup.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(setup.os, "killpg", lambda pid, value: signals.append((pid, value)))
    monkeypatch.setattr(setup, "PROCESS_TERMINATION_TIMEOUT", 0.01, raising=False)
    task = asyncio.create_task(setup._run_command("prepare-tunnel-engine", ["/bin/bash", "builder"],
                                                timeout=0.01 if failure == "timeout" else None))
    await asyncio.wait_for(entered.wait(), timeout=1)
    if failure == "cancel":
        task.cancel()
    with pytest.raises(TimeoutError if failure == "timeout" else asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert signals == [(process.pid, signal.SIGTERM)] + ([(process.pid, signal.SIGKILL)] if resists_term else [])
    assert process.returncode == 0


@pytest.mark.asyncio
async def test_successful_setup_removes_old_builds_but_keeps_the_one_a_running_session_may_use(project, monkeypatch, capsys):
    import os
    import time
    args, _ = project
    old = time.time() - 3 * 24 * 3600
    previous = args.home / "build" / "previous" / "Runner.app"
    stale = args.home / "build" / "stale"
    previous.mkdir(parents=True)
    (stale / "Big").mkdir(parents=True)
    (stale / "Big" / "blob").write_bytes(b"s" * 64)
    for path in (previous.parent, stale):
        os.utime(path, (old, old))
    (args.home / "runner.json").write_text(json.dumps({"udid": "USB-1", "bundleId": "org.example.WDA.xctrunner",
                                                        "appPath": str(previous)}))

    async def fake_run(step, command):
        if step == "build-for-testing":
            product(command)

    monkeypatch.setattr(setup, "_run_command", fake_run)
    assert await setup.build_runner(args) == 0
    current = Path(json.loads((args.home / "runner.json").read_text())["appPath"])
    assert current.is_dir() and previous.is_dir() and not stale.exists()
    steps = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {"step": "removed-old-builds", "count": 1, "bytes": 64} in steps


@pytest.mark.asyncio
async def test_failed_setup_removes_no_build(project, monkeypatch):
    import os
    import time
    args, _ = project
    stale = args.home / "build" / "stale"
    stale.mkdir(parents=True)
    old = time.time() - 3 * 24 * 3600
    os.utime(stale, (old, old))

    async def fake_run(step, command):
        if step == "build-for-testing":
            raise RuntimeError("build failed")

    monkeypatch.setattr(setup, "_run_command", fake_run)
    with pytest.raises(RuntimeError):
        await setup.build_runner(args)
    assert stale.exists()
