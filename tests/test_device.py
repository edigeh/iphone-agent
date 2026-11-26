"""Unit tests for USB selection and the device service contract."""

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from iphone_agent import device as module  # noqa: E402


class MuxDevice:
    def __init__(self, serial, connection_type="USB", devid=1):
        self.serial = serial
        self.connection_type = connection_type
        self.devid = devid

    def matches_udid(self, udid):
        return self.serial.replace("-", "") == udid.replace("-", "")


class Lockdown:
    def __init__(self, paired=True):
        self.paired = paired
        self.values = {"DeviceName": "Test iPhone"}
        self.keys = {"TimeZone": "America/Sao_Paulo", "TimeIntervalSince1970": 1_700_000_000.9}
        self.timezone = None
        self.closed = False

    async def get_value(self, key=None):
        return self.values if key is None else self.keys[key]

    async def set_timezone(self, timezone):
        self.timezone = timezone

    async def close(self):
        self.closed = True


class FakeService:
    def __init__(self, client):
        self.client = client
        self.calls = []
        self.contents = {"/Documents/a.txt": b"hello"}
        self.attrs = {
            "/Documents": {"st_ifmt": "S_IFDIR"},
            "/Documents/a.txt": {"st_ifmt": "S_IFREG", "st_size": 5, "st_mtime": 123},
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def browse(self):
        return [{"CFBundleIdentifier": "example.app"}]

    async def install_from_local(self, path):
        self.calls.append(("install", path))

    async def uninstall(self, bundle_id):
        self.calls.append(("uninstall", bundle_id))

    async def stat(self, path):
        if path not in self.attrs:
            raise module.AfcFileNotFoundError("missing", 8, path)
        return self.attrs[path]

    async def listdir(self, path):
        self.calls.append(("listdir", path))
        return ["a.txt"]

    async def get_file_contents(self, path):
        self.calls.append(("read", path))
        return self.contents[path]

    async def set_file_contents(self, path, data):
        self.calls.append(("write", path, data))
        self.contents[path] = data

    async def rm_single(self, path):
        self.calls.append(("delete", path))


@pytest.fixture
def fake(monkeypatch):
    client = Lockdown()
    devices = [MuxDevice("abc-123"), MuxDevice("network", "Network")]
    services = []
    connections = []

    async def list_devices():
        return devices

    async def connect(**kwargs):
        connections.append(kwargs)
        return client

    def service_factory(connected):
        service = FakeService(connected)
        services.append(service)
        return service

    class HouseArrest:
        @classmethod
        async def create(cls, connected, bundle_id):
            assert bundle_id == "example.app"
            return service_factory(connected)

    monkeypatch.setattr(module, "list_devices", list_devices)
    monkeypatch.setattr(module, "create_using_usbmux", connect)
    monkeypatch.setattr(module, "InstallationProxyService", service_factory)
    monkeypatch.setattr(module, "AfcService", service_factory)
    monkeypatch.setattr(module, "HouseArrestService", HouseArrest)
    return SimpleNamespace(client=client, devices=devices, services=services, connections=connections)


@pytest.mark.asyncio
async def test_usb_discovery_and_explicit_selection(fake):
    assert await module.DeviceServices.discover() == [
        {"udid": "abc-123", "connection_type": "USB", "device_id": 1}
    ]
    device = module.DeviceServices("abc123")
    assert (await device.info())["DeviceName"] == "Test iPhone"
    assert fake.connections == [{"serial": "abc-123", "connection_type": "USB", "autopair": False,
                                 "label": "Local iPhone Controller"}]
    await device.close()
    assert fake.client.closed


@pytest.mark.asyncio
async def test_device_metadata_is_json_serializable(fake, monkeypatch):
    fake.client.values["BinaryValue"] = b"abc"
    device = module.DeviceServices("abc-123")
    info = await device.info()
    assert info["BinaryValue"] == {"base64": "YWJj"}
    service = FakeService(fake.client)
    service.attrs["/Documents/a.txt"]["st_mtime"] = datetime(2026, 1, 2)
    monkeypatch.setattr(module, "AfcService", lambda client: service)
    entries = await device.files_list("/Documents")
    assert entries[0]["modified"] == "2026-01-02T00:00:00"


@pytest.mark.asyncio
async def test_concurrent_calls_share_one_connection(fake):
    device = module.DeviceServices("abc-123")
    await asyncio.gather(device.info(), device.timezone_get(), device.time())
    assert len(fake.connections) == 1
    await device.close()


@pytest.mark.asyncio
async def test_missing_ambiguous_and_unpaired_fail(fake):
    with pytest.raises(RuntimeError, match="not connected"):
        await module.DeviceServices("other").info()
    fake.devices.append(MuxDevice("abc123", devid=2))
    with pytest.raises(RuntimeError, match="ambiguous"):
        await module.DeviceServices("abc-123").info()
    fake.devices.pop()
    fake.client.paired = False
    with pytest.raises(RuntimeError, match="not paired"):
        await module.DeviceServices("abc-123").info()
    assert fake.client.closed


@pytest.mark.asyncio
async def test_packages_install_uninstall_and_time(fake, tmp_path):
    device = module.DeviceServices("abc-123")
    assert (await device.packages())[0]["CFBundleIdentifier"] == "example.app"
    package = tmp_path / "sample.ipa"
    package.write_bytes(b"ipa")
    await device.install(str(package))
    await device.uninstall("example.app")
    assert ("install", package) in fake.services[-2].calls
    assert ("uninstall", "example.app") in fake.services[-1].calls
    assert await device.time() == 1_700_000_000
    assert await device.timezone_get() == "America/Sao_Paulo"
    await device.timezone_set("Europe/London")
    assert fake.client.timezone == "Europe/London"
    with pytest.raises(ValueError, match="Unknown"):
        await device.timezone_set("Invalid/Timezone")


@pytest.mark.asyncio
async def test_files_afc_and_house_arrest(fake):
    device = module.DeviceServices("abc-123")
    assert await device.files_read("/Documents/a.txt") == b"hello"
    assert (await device.files_list("/Documents"))[0]["name"] == "a.txt"
    await device.files_write("/Documents/new.txt", b"new", bundle_id="example.app")
    assert ("write", "/Documents/new.txt", b"new") in fake.services[-1].calls
    await device.files_delete("/Documents/a.txt")
    assert ("delete", "/Documents/a.txt") in fake.services[-1].calls


@pytest.mark.asyncio
async def test_paths_and_links_are_confined(fake):
    device = module.DeviceServices("abc-123")
    for path in ("relative", "/Documents/../private", "/Documents/./x", "/a\\b", "/a\x00b"):
        with pytest.raises(ValueError):
            await device.files_read(path)
    with pytest.raises(ValueError, match="root"):
        await device.files_delete("/")
    with pytest.raises(ValueError, match="root"):
        await device.files_write("/", b"x")
    service = FakeService(fake.client)
    service.attrs["/Documents"] = {"st_ifmt": "S_IFLNK", "LinkTarget": "/private"}
    with pytest.raises(RuntimeError, match="Symbolic link"):
        await device._check_no_symlink(service, "/Documents/a.txt")
