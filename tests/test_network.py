"""Explicit network access stays bound to CoreDevice hardware identity."""

import asyncio
import json
from pathlib import Path

import pytest

from iphone_agent import network


UDID = "DEVICE-UDID"
IP = "fd81:8c21:709c::1"


def device_record(*, udid=UDID, ip=IP, pairing="paired", tunnel="connected", identifier="unrelated-CoreDevice-UUID"):
    return {
        "identifier": identifier,
        "hardwareProperties": {"udid": udid},
        "connectionProperties": {"pairingState": pairing, "tunnelState": tunnel,
                                 "tunnelIPAddress": ip, "transportType": "network"},
    }


def inventory(*records, **properties):
    return {"info": {"outcome": "success", "commandType": "devicectl.list.devices"},
            "result": {"devices": list(records) if records else [device_record(**properties)]}}


def selected_details(**properties):
    return {"result": device_record(**properties)}


class FinishedProcess:
    pid = 12345
    returncode = 0

    async def wait(self):
        return 0


@pytest.fixture
def devicectl(monkeypatch):
    state = {"data": inventory(), "commands": [], "outputs": [], "lock_data": {"info": {"outcome": "success", "commandType": "devicectl.device.info.lockState"}, "result": {
        "deviceIdentifier": "unrelated-CoreDevice-UUID", "passcodeRequired": False, "unlockedSinceBoot": True,
    }}}

    async def execute(*command, **kwargs):
        state["commands"].append((command, kwargs))
        output = Path(command[command.index("--json-output") + 1])
        state["outputs"].append(output)
        if command[:5] == ("xcrun", "devicectl", "device", "info", "lockState"):
            data = state["lock_data"]
        else:
            assert command[:4] == ("xcrun", "devicectl", "list", "devices")
            sequence = state.get("inventory_sequence", [])
            data = sequence.pop(0) if sequence else state["data"]
        output.write_text(json.dumps(data))
        return FinishedProcess()

    monkeypatch.setattr(network.asyncio, "create_subprocess_exec", execute)
    return state


@pytest.mark.parametrize("url", [
    "http://example.com:8100", "http://user:pass@[::1]:8100",
    "http://[::1]:8100/path", "http://[::1]:8100/",
    "http://[::1]:8100?", "http://[::1]:8100#",
    "http://[fe80::1%25en0]:8100", "ftp://[::1]:8100",
    "http://127.0.0.1:0", "http://127.0.0.1:65536",
])
def test_rejects_non_origin_or_non_literal_ip(url):
    with pytest.raises(ValueError):
        network.NetworkBinding(UDID, url)


@pytest.mark.asyncio
async def test_exact_hardware_udid_and_tunnel_ip_bind_origin(devicectl):
    binding = network.NetworkBinding(UDID, "http://[fd81:8c21:709c:0:0:0:0:1]:8100")
    assert binding.origin == "http://[fd81:8c21:709c::1]:8100"
    assert binding.host == IP
    assert binding.port == 8100
    assert await binding.validate() is binding
    command, kwargs = devicectl["commands"][0]
    assert command[:4] == ("xcrun", "devicectl", "list", "devices")
    assert "--device" not in command
    assert "--quiet" in command
    assert "--json-output" in command
    assert kwargs["start_new_session"] is True
    assert not devicectl["outputs"][0].exists()


@pytest.mark.parametrize("inactive", ["disconnected", "connecting", "missing-ip"])
@pytest.mark.parametrize("locked", [False, True])
async def test_cold_start_bootstraps_paired_target_once_then_resolves_only_fresh_inventory(devicectl, inactive, locked):
    changed = "fd81:8c21:709c::2"
    devicectl["inventory_sequence"] = [inventory(tunnel="connected" if inactive == "missing-ip" else inactive, ip=None), inventory(ip=changed)]
    devicectl["lock_data"]["result"]["passcodeRequired"] = locked
    binding = await network.NetworkBinding.for_device(UDID)
    assert binding.origin == f"http://[{changed}]:8100"
    commands = [command for command, _ in devicectl["commands"]]
    assert commands[0][:4] == commands[2][:4] == ("xcrun", "devicectl", "list", "devices")
    assert commands[1][:5] == ("xcrun", "devicectl", "device", "info", "lockState")
    assert commands[1][commands[1].index("--device") + 1] == UDID
    assert all(command[command.index("--timeout") + 1] == "15" for command in commands)


async def test_bootstrap_wrong_lock_identifier_fails_before_inventory_recheck(devicectl):
    devicectl["data"] = inventory(tunnel="disconnected", ip=None)
    devicectl["lock_data"]["result"]["deviceIdentifier"] = "OTHER-CORE-ID"
    with pytest.raises(network.NetworkBindingError, match="lock state") as caught:
        await network.NetworkBinding.for_device(UDID)
    assert not network.is_recoverable_error(caught.value)
    assert len(devicectl["commands"]) == 2


@pytest.mark.parametrize("failure", ["absent", "unpaired", "malformed", "duplicate-uid", "collision", "bad-outcome"])
async def test_untrusted_inventory_never_bootstraps_a_connection(devicectl, failure):
    payload = inventory(tunnel="disconnected", ip=None)
    if failure == "absent":
        payload["result"]["devices"] = []
    elif failure == "unpaired":
        payload["result"]["devices"][0]["connectionProperties"]["pairingState"] = "unpaired"
    elif failure == "malformed":
        del payload["result"]["devices"][0]["identifier"]
    elif failure == "duplicate-uid":
        payload["result"]["devices"].append(device_record(identifier="duplicate-core-id"))
    elif failure == "collision":
        payload = inventory(device_record(), device_record(udid="OTHER", identifier="foreign-core-id"))
    else:
        payload["info"]["outcome"] = "failed"
    devicectl["data"] = payload
    with pytest.raises(network.NetworkBindingError) as caught:
        await network.NetworkBinding.for_device(UDID)
    assert isinstance(caught.value, network.CoreDeviceUnavailableError) is (failure == "absent")
    assert len(devicectl["commands"]) == 1
    assert devicectl["commands"][0][0][:4] == ("xcrun", "devicectl", "list", "devices")


@pytest.mark.parametrize("recheck", ["still-inactive", "changed-uid", "unpaired", "collision"])
async def test_bootstrap_never_uses_initial_or_untrusted_rechecked_address(devicectl, recheck):
    after = inventory()
    if recheck == "still-inactive":
        after = inventory(tunnel="disconnected", ip=None)
    elif recheck == "changed-uid":
        after = inventory(udid="OTHER")
    elif recheck == "unpaired":
        after = inventory(pairing="unpaired")
    else:
        after = inventory(device_record(), device_record(udid="OTHER", identifier="foreign-core-id"))
    devicectl["inventory_sequence"] = [inventory(tunnel="disconnected", ip="192.0.2.55"), after]
    with pytest.raises(network.NetworkBindingError) as caught:
        await network.NetworkBinding.for_device(UDID)
    assert network.is_recoverable_error(caught.value) is (recheck in {"still-inactive", "changed-uid"})
    assert len(devicectl["commands"]) == 3  # One bootstrap, followed by one fresh inventory.


async def test_existing_wda_and_media_preflights_never_bootstrap_inactive_binding(devicectl, monkeypatch):
    devicectl["data"] = inventory(tunnel="disconnected", ip=None)
    binding = network.NetworkBinding(UDID, f"http://[{IP}]:8100")

    async def forbidden(*args, **kwargs):
        pytest.fail("An inactive preflight must not send a WDA mutation")

    monkeypatch.setattr(network.WDAClient, "call", forbidden)
    operations = [binding.validate, lambda: binding.connection_factory(UDID, 9200).open(),
                  lambda: network.BoundWDAClient(binding).call("POST", "/mutation", {})]
    for operation in operations:
        with pytest.raises(network.CoreDeviceUnavailableError):
            await operation()
    assert len(devicectl["commands"]) == 3
    assert all(command[:4] == ("xcrun", "devicectl", "list", "devices") for command, _ in devicectl["commands"])
    assert binding.host == IP


@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_bootstrap_lock_query_interruption_reaps_only_owned_child(devicectl, monkeypatch, interruption):
    devicectl["data"] = inventory(tunnel="disconnected", ip=None)
    execute_inventory = network.asyncio.create_subprocess_exec
    real_wait_for = asyncio.wait_for
    created, released = asyncio.Event(), asyncio.Event()
    killed = []

    class Process:
        pid = 9876
        returncode = None
        reaped = False

        async def wait(self):
            await released.wait()
            self.returncode = -15
            self.reaped = True
            return -15

    process = Process()

    async def execute(*command, **kwargs):
        if command[:4] == ("xcrun", "devicectl", "list", "devices"):
            return await execute_inventory(*command, **kwargs)
        assert command[:5] == ("xcrun", "devicectl", "device", "info", "lockState")
        devicectl["commands"].append((command, kwargs))
        devicectl["outputs"].append(Path(command[command.index("--json-output") + 1]))
        created.set()
        return process

    def killpg(pid, signal):
        killed.append((pid, signal))
        released.set()

    async def short_deadline(awaitable, timeout):
        return await real_wait_for(awaitable, 0.01 if timeout == 15 else timeout)

    monkeypatch.setattr(network.asyncio, "create_subprocess_exec", execute)
    monkeypatch.setattr(network.os, "killpg", killpg)
    if interruption == "timeout":
        monkeypatch.setattr(network.asyncio, "wait_for", short_deadline)
    task = asyncio.create_task(network.NetworkBinding.for_device(UDID))
    await created.wait()
    if interruption == "cancel":
        task.cancel()
    with pytest.raises(asyncio.CancelledError if interruption == "cancel" else network.CoreDeviceTimeoutError):
        await task
    assert killed == [(9876, network.signal.SIGTERM)] and process.reaped
    assert len(devicectl["commands"]) == 2
    assert all(not path.parent.exists() for path in devicectl["outputs"])


@pytest.mark.parametrize("corruption", [
    "missing-info", "failure", "wrong-command", "null-command", "missing-devices", "devices-object",
    "nonobject-record", "missing-hardware", "boolean-udid", "missing-connection", "missing-state",
    "duplicate-hardware", "duplicate-hardware-address-change", "duplicate-core-identifier", "contradictory-udid",
    "malformed-foreign-record", "malformed-foreign-ip",
])
async def test_invalid_inventory_is_fatal_without_retry_or_mutation(devicectl, monkeypatch, corruption):
    payload = inventory()
    devices = payload["result"]["devices"]
    if corruption == "missing-info":
        del payload["info"]
    elif corruption == "failure":
        payload["info"]["outcome"] = "failed"
    elif corruption in ("wrong-command", "null-command"):
        payload["info"]["commandType"] = None if corruption == "null-command" else "devicectl.device.info.details"
    elif corruption == "missing-devices":
        del payload["result"]["devices"]
    elif corruption == "devices-object":
        payload["result"]["devices"] = {}
    elif corruption == "nonobject-record":
        devices.append("not a device")
    elif corruption == "missing-hardware":
        del devices[0]["hardwareProperties"]
    elif corruption == "boolean-udid":
        devices[0]["hardwareProperties"]["udid"] = True
    elif corruption == "missing-connection":
        del devices[0]["connectionProperties"]
    elif corruption == "missing-state":
        del devices[0]["connectionProperties"]["tunnelState"]
    elif corruption in ("duplicate-hardware", "duplicate-hardware-address-change"):
        devices.append(device_record(identifier="other-core-id", ip=IP if corruption == "duplicate-hardware" else "192.0.2.2"))
    elif corruption == "duplicate-core-identifier":
        devices.append(device_record(udid="OTHER"))
    elif corruption == "contradictory-udid":
        devices[0]["udid"] = "OTHER"
    elif corruption == "malformed-foreign-record":
        devices.append({"identifier": "other-core-id", "hardwareProperties": {}, "connectionProperties": {}})
    else:
        devices.append(device_record(udid="OTHER", identifier="other-core-id", ip="not-an-ip"))
    devicectl["data"] = payload
    requests = []

    async def request(*args, **kwargs):
        requests.append(True)
        return {"done": True}

    monkeypatch.setattr(network.WDAClient, "call", request)
    client = network.BoundWDAClient(network.NetworkBinding(UDID, f"http://[{IP}]:8100"))
    with pytest.raises(network.NetworkBindingError) as caught:
        await client.call("POST", "/mutation", {})
    assert not network.is_recoverable_error(caught.value)
    assert requests == [] and len(devicectl["commands"]) == 1


async def test_inventory_command_type_may_be_omitted_but_each_read_is_fresh(devicectl):
    del devicectl["data"]["info"]["commandType"]
    binding = network.NetworkBinding(UDID, f"http://[{IP}]:8100")
    assert await binding.validate() is binding
    assert await binding.validate() is binding
    assert len(devicectl["commands"]) == 2
    assert len(set(devicectl["outputs"])) == 2
    assert all(not path.exists() for path in devicectl["outputs"])


@pytest.mark.parametrize("absent", [True, False])
async def test_absent_or_disconnected_exact_target_is_transient_and_never_sends_wda(devicectl, monkeypatch, absent):
    devicectl["data"] = inventory(udid="OTHER" if absent else UDID, tunnel="disconnected")

    async def forbidden(*args, **kwargs):
        pytest.fail("An absent/disconnected target cannot send a WDA action")

    monkeypatch.setattr(network.WDAClient, "call", forbidden)
    client = network.BoundWDAClient(network.NetworkBinding(UDID, f"http://[{IP}]:8100"))
    with pytest.raises(network.CoreDeviceUnavailableError):
        await client.call("POST", "/mutation", {})
    assert len(devicectl["commands"]) == 1
    assert client.binding.host == IP


async def test_foreign_record_matching_old_ip_never_masks_exact_target_ip_change(devicectl, monkeypatch):
    changed = "fd81:8c21:709c::2"
    devicectl["data"] = inventory(device_record(udid="OTHER", identifier="foreign-core-id"), device_record(ip=changed))
    assert (await network.NetworkBinding.for_device(UDID)).host == changed
    binding = network.NetworkBinding(UDID, f"http://[{IP}]:8100")

    async def forbidden(*args, **kwargs):
        pytest.fail("The old WDA binding cannot be retargeted or used after its address changes")

    monkeypatch.setattr(network.WDAClient, "call", forbidden)
    with pytest.raises(network.NetworkAddressChangedError):
        await network.BoundWDAClient(binding).call("POST", "/mutation", {})
    with pytest.raises(network.NetworkAddressChangedError):
        await binding.connection_factory(UDID, 9200).open()
    assert binding.host == IP and binding.origin == f"http://[{IP}]:8100"


@pytest.mark.parametrize("foreign_ip", [IP, "fd81:8c21:709c:0:0:0:0:1"])
async def test_duplicate_active_ip_across_distinct_devices_is_fatal_before_wda_or_media(devicectl, monkeypatch, foreign_ip):
    devicectl["data"] = inventory(device_record(), device_record(udid="OTHER", identifier="foreign-core-id", ip=foreign_ip))
    requests = []

    async def request(*args, **kwargs):
        requests.append(True)
        return {"done": True}

    monkeypatch.setattr(network.WDAClient, "call", request)
    binding = network.NetworkBinding(UDID, f"http://[{IP}]:8100")
    operations = [lambda: network.NetworkBinding.for_device(UDID),
                  lambda: network.BoundWDAClient(binding).call("POST", "/mutation", {}),
                  lambda: binding.connection_factory(UDID, 9200).open()]
    for operation in operations:
        with pytest.raises(network.NetworkBindingError, match="IP") as caught:
            await operation()
        assert not network.is_recoverable_error(caught.value)
    assert requests == [] and len(devicectl["commands"]) == 3
    assert binding.host == IP


async def test_disconnected_foreign_record_with_cached_ip_does_not_duplicate_active_route(devicectl):
    devicectl["data"] = inventory(device_record(), device_record(udid="OTHER", identifier="foreign-core-id", tunnel="disconnected"))
    assert (await network.NetworkBinding.for_device(UDID)).host == IP


async def test_selected_network_record_is_found_among_foreign_records_without_wired_requirement(devicectl):
    devicectl["data"] = inventory(device_record(udid="OTHER", identifier="foreign-core-id", ip="192.0.2.3"), device_record())
    binding = network.NetworkBinding(UDID, f"http://[{IP}]:8100")
    assert await binding.validate() is binding
    assert binding.host == IP


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), "15"])
def test_identity_deadline_must_be_finite_positive_numeric(timeout):
    with pytest.raises(ValueError, match="timeout"):
        network.NetworkBinding(UDID, f"http://[{IP}]:8100", timeout=timeout)


async def test_resolves_live_tunnel_origin_for_exact_device(devicectl):
    binding = await network.NetworkBinding.for_device(UDID)
    assert binding.origin == f"http://[{IP}]:8100"
    devicectl["data"] = inventory(udid="OTHER")
    with pytest.raises(network.NetworkBindingError, match="hardware UDID"):
        await network.NetworkBinding.for_device(UDID)


@pytest.mark.parametrize("payload,recoverable", [
    (inventory(ip="fd81:8c21:709c::2"), True),
    (inventory(ip=None), True),
    (inventory(tunnel="disconnected"), True),
    (inventory(udid="OTHER", ip=None), True),
    (inventory(pairing="unpaired", ip=None), False),
    (inventory(ip="malformed"), False),
    (inventory(ip=True), False),
    (inventory(tunnel="unknown"), False),
])
async def test_binding_distinguishes_recoverable_transport_from_identity_errors(devicectl, payload, recoverable):
    devicectl["data"] = payload
    binding = network.NetworkBinding(UDID, f"http://[{IP}]:8100")
    with pytest.raises(network.NetworkBindingError) as caught:
        await binding.validate()
    assert network.is_recoverable_error(caught.value) is recoverable
    assert binding.host == IP  # Validation never repoints an existing client.


@pytest.mark.parametrize("failure", ["disconnect", "payload", "timeout"])
async def test_bound_wda_transport_failure_is_typed_and_never_replays_post(devicectl, monkeypatch, failure):
    import aiohttp
    calls = []

    async def effect_then_disconnect(self, method, path, body=None):
        calls.append((method, path, body))
        error = {"disconnect": aiohttp.ServerDisconnectedError, "payload": aiohttp.ClientPayloadError,
                 "timeout": TimeoutError}[failure]
        raise error("response lost after action")

    monkeypatch.setattr(network.WDAClient, "call", effect_then_disconnect)
    client = network.BoundWDAClient(network.NetworkBinding(UDID, f"http://[{IP}]:8100"))
    with pytest.raises(network.WDATransportError) as caught:
        await client.call("POST", "/mobilerun/actions", [{"type": "pointerUp"}])
    assert network.is_recoverable_error(caught.value)
    assert isinstance(caught.value, ConnectionError)
    assert len(calls) == 1 and len(devicectl["commands"]) == 1


@pytest.mark.parametrize("passcode,since_boot,expected", [(False, True, False), (True, True, True), (False, False, True)])
async def test_lock_state_is_bound_to_fresh_exact_device_and_does_not_claim_awake(devicectl, passcode, since_boot, expected):
    devicectl["lock_data"]["result"].update(passcodeRequired=passcode, unlockedSinceBoot=since_boot)
    binding = network.NetworkBinding(UDID, f"http://[{IP}]:8100")
    assert await binding.passcode_required() is expected
    assert devicectl["commands"][0][0][:4] == ("xcrun", "devicectl", "list", "devices")
    command = devicectl["commands"][1][0]
    assert command[:5] == ("xcrun", "devicectl", "device", "info", "lockState")
    assert command[command.index("--device") + 1] == UDID


@pytest.mark.parametrize("field,value", [("deviceIdentifier", "OTHER"), ("passcodeRequired", "false"), ("unlockedSinceBoot", None)])
async def test_lock_state_rejects_malformed_or_other_device_readback(devicectl, field, value):
    devicectl["lock_data"]["result"][field] = value
    binding = network.NetworkBinding(UDID, f"http://[{IP}]:8100")
    with pytest.raises(network.NetworkBindingError):
        await binding.passcode_required()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload, expected", [
    (inventory(udid="OTHER"), "hardware UDID"),
    (inventory(ip="fd81:8c21:709c::2"), "current tunnel IP"),
    (inventory(pairing="unpaired"), "connected paired"),
    (inventory(tunnel="disconnected"), "connected paired"),
    ({"info": {"outcome": "success"}, "result": {"devices": [{"identifier": UDID}]}}, "hardware UDID"),
    (inventory({"identifier": "core-id", "hardwareProperties": {"udid": UDID}, "connectionProperties": {}}), "connection properties"),
])
async def test_binding_fails_closed_on_identity_or_tunnel_mismatch(devicectl, payload, expected):
    devicectl["data"] = payload
    binding = network.NetworkBinding(UDID, f"http://[{IP}]:8100")
    with pytest.raises(network.NetworkBindingError, match=expected):
        await binding.validate()
    assert len(devicectl["commands"]) == 1


@pytest.mark.asyncio
async def test_bound_direct_connection_rechecks_before_open(devicectl):
    binding = network.NetworkBinding(UDID, f"http://[{IP}]:8100")
    with pytest.raises(network.NetworkBindingError, match="UDID"):
        binding.connection_factory("OTHER", 9200)
    connection = binding.connection_factory(UDID, 9200)
    assert connection.host == IP and connection.port == 9200
    await connection.open()
    assert len(devicectl["commands"]) == 1
    devicectl["data"] = inventory(ip="fd81:8c21:709c::2")
    with pytest.raises(network.NetworkBindingError, match="current tunnel IP"):
        await connection.open()
    await connection.close()


@pytest.mark.asyncio
async def test_bound_wda_client_rechecks_each_request(devicectl, monkeypatch):
    calls = []

    async def fake_call(self, method, path, body=None):
        calls.append((method, path))
        return {"ready": True}

    monkeypatch.setattr(network.WDAClient, "call", fake_call)
    binding = network.NetworkBinding(UDID, f"http://[{IP}]:8100")
    client = network.BoundWDAClient(binding)
    assert await client.call("GET", "/status") == {"ready": True}
    assert len(devicectl["commands"]) == 1
    devicectl["data"] = inventory(udid="OTHER")
    with pytest.raises(network.NetworkBindingError, match="hardware UDID"):
        await client.call("GET", "/status")
    assert len(calls) == 1
    assert len(devicectl["commands"]) == 2


@pytest.mark.parametrize("timeout", [0.1, 30, 300])
async def test_bound_proxy_forwards_request_budget_after_one_identity_preflight(devicectl, monkeypatch, timeout):
    calls = []

    async def send(self, method, path, body=None, *, request_timeout=None):
        calls.append((method, path, body, request_timeout))
        return {"state": "connected"}

    monkeypatch.setattr(network.WDAClient, "call", send)
    client = network.BoundWDAClient(network.NetworkBinding(UDID, f"http://[{IP}]:8100"))
    assert await client.proxy_connect("socks5h://proxy:1080", timeout) == {"state": "connected"}
    assert calls == [("POST", "/mobilerun/socks5/connect", {"uri": "socks5h://proxy:1080", "timeout": timeout}, timeout + 5)]
    assert len(devicectl["commands"]) == 1
    assert client.timeout == 45


async def test_bound_proxy_rejects_invalid_server_budget_before_identity_preflight(devicectl, monkeypatch):
    async def forbidden_transport(*args, **kwargs):
        pytest.fail("invalid proxy budget reached HTTP transport")

    monkeypatch.setattr(network.WDAClient, "call", forbidden_transport)
    client = network.BoundWDAClient(network.NetworkBinding(UDID, f"http://[{IP}]:8100"))
    with pytest.raises(ValueError, match="timeout"):
        await client.proxy_connect("socks5h://proxy:1080", 301)
    assert devicectl["commands"] == []


async def test_budgeted_proxy_lost_response_is_unknown_and_never_retried(devicectl, monkeypatch):
    effects = []

    async def effect_then_timeout(self, method, path, body=None, *, request_timeout=None):
        effects.append((method, path, body, request_timeout))
        raise TimeoutError("proxy started but response was lost")

    monkeypatch.setattr(network.WDAClient, "call", effect_then_timeout)
    client = network.BoundWDAClient(network.NetworkBinding(UDID, f"http://[{IP}]:8100"))
    with pytest.raises(network.WDATransportError, match="outcome may be unknown") as caught:
        await client.proxy_connect("socks5h://proxy:1080", 300)
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert effects == [("POST", "/mobilerun/socks5/connect", {"uri": "socks5h://proxy:1080", "timeout": 300}, 305)]
    assert len(devicectl["commands"]) == 1


async def test_binding_failure_during_close_still_closes_http(devicectl):
    binding = network.NetworkBinding(UDID, f"http://[{IP}]:8100")
    client = network.BoundWDAClient(binding)
    await client.open()
    session = client._http
    client._session_id = "our-session"
    devicectl["data"] = inventory(udid="OTHER")
    with pytest.raises(network.NetworkBindingError):
        await client.close()
    assert session.closed
    assert client._http is None


@pytest.mark.asyncio
async def test_devicectl_timeout_terminates_only_its_process_group(monkeypatch):
    event = asyncio.Event()
    killed = []

    class HangingProcess:
        pid = 9876
        returncode = None

        async def wait(self):
            await event.wait()
            self.returncode = -15
            return self.returncode

    async def execute(*_command, **_kwargs):
        return HangingProcess()

    def killpg(pid, sig):
        killed.append((pid, sig))
        event.set()

    monkeypatch.setattr(network.asyncio, "create_subprocess_exec", execute)
    monkeypatch.setattr(network.os, "killpg", killpg)
    with pytest.raises(network.NetworkBindingError, match="timed out"):
        await network._coredevice_details_once(UDID, 0.01)
    assert killed == [(9876, network.signal.SIGTERM)]


async def test_inventory_cancellation_reaps_owned_child_and_removes_private_output(monkeypatch):
    created, released = asyncio.Event(), asyncio.Event()
    killed, outputs = [], []

    class Process:
        pid = 9876
        returncode = None
        reaped = False

        async def wait(self):
            await released.wait()
            self.returncode = -15
            self.reaped = True
            return -15

    process = Process()

    async def execute(*command, **kwargs):
        assert command[:4] == ("xcrun", "devicectl", "list", "devices")
        outputs.append(Path(command[command.index("--json-output") + 1]))
        created.set()
        return process

    def killpg(pid, signal):
        killed.append((pid, signal))
        released.set()

    monkeypatch.setattr(network.asyncio, "create_subprocess_exec", execute)
    monkeypatch.setattr(network.os, "killpg", killpg)
    task = asyncio.create_task(network._coredevice_details_once(UDID, 15))
    await created.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [(9876, network.signal.SIGTERM)] and process.reaped
    assert len(outputs) == 1 and not outputs[0].parent.exists()


@pytest.mark.parametrize("corruption", ["outcome", "command"])
async def test_lock_state_keeps_its_separate_validated_command_envelope(devicectl, corruption):
    if corruption == "outcome":
        devicectl["lock_data"]["info"]["outcome"] = "failed"
    else:
        devicectl["lock_data"]["info"]["commandType"] = "devicectl.list.devices"
    with pytest.raises(network.NetworkBindingError) as caught:
        await network.NetworkBinding(UDID, f"http://[{IP}]:8100").passcode_required()
    assert not network.is_recoverable_error(caught.value)
    assert len(devicectl["commands"]) == 2
    assert devicectl["commands"][1][0][:5] == ("xcrun", "devicectl", "device", "info", "lockState")


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["term", "kill"])
async def test_details_timeout_reaps_owned_process_before_single_retry(monkeypatch, termination):
    released = asyncio.Event()
    killed = []
    processes = []

    class HangingProcess:
        pid = 9876
        returncode = None
        reaped = False

        async def wait(self):
            await released.wait()
            self.returncode = -15 if termination == "term" else -9
            self.reaped = True
            return self.returncode

    async def execute(*command, **kwargs):
        assert kwargs["start_new_session"] is True
        if not processes:
            process = HangingProcess()
        else:
            assert len(processes) == 1
            assert processes[0].reaped and processes[0].returncode is not None
            output = Path(command[command.index("--json-output") + 1])
            output.write_text(json.dumps(inventory()))
            process = FinishedProcess()
        processes.append(process)
        return process

    def killpg(pid, sig):
        killed.append((pid, sig))
        if termination == "term" or sig == network.signal.SIGKILL:
            released.set()

    monkeypatch.setattr(network.asyncio, "create_subprocess_exec", execute)
    monkeypatch.setattr(network.os, "killpg", killpg)
    assert await network._coredevice_details(UDID, 0.01) == selected_details()
    assert len(processes) == 2
    expected = [(9876, network.signal.SIGTERM)]
    if termination == "kill":
        expected.append((9876, network.signal.SIGKILL))
    assert killed == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["resolve", "validate", "post", "post_timeout"])
async def test_details_retry_is_preflight_only_and_used_by_each_entry(monkeypatch, entry):
    reads = []
    requests = []

    async def read(udid, timeout):
        reads.append((udid, timeout))
        if len(reads) == 1:
            raise network.CoreDeviceTimeoutError("CoreDevice device details timed out")
        return selected_details()

    async def request(self, method, path, body=None):
        assert len(reads) == 2
        requests.append((method, path, body))
        if entry == "post_timeout":
            raise TimeoutError("WDA response timed out after mutation")
        return {"done": True}

    monkeypatch.setattr(network, "_coredevice_details_once", read)
    monkeypatch.setattr(network.WDAClient, "call", request)
    binding = network.NetworkBinding(UDID, f"http://[{IP}]:8100")
    if entry == "resolve":
        assert (await network.NetworkBinding.for_device(UDID)).host == IP
    elif entry == "validate":
        assert await binding.validate() is binding
    elif entry == "post_timeout":
        with pytest.raises(network.WDATransportError) as caught:
            await network.BoundWDAClient(binding).call("POST", "/mutation", {"value": 1})
        assert isinstance(caught.value.__cause__, TimeoutError)
    else:
        assert await network.BoundWDAClient(binding).call("POST", "/mutation", {"value": 1}) == {"done": True}
    assert reads == [(UDID, 15.0), (UDID, 15.0)]
    assert requests == ([("POST", "/mutation", {"value": 1})] if entry.startswith("post") else [])


@pytest.mark.asyncio
async def test_two_details_timeouts_fail_without_sending_wda_request(monkeypatch):
    reads = []

    async def read(udid, timeout):
        reads.append((udid, timeout))
        raise network.CoreDeviceTimeoutError("CoreDevice device details timed out")

    async def request(*_args, **_kwargs):
        pytest.fail("WDA must not run after failed preflight")

    monkeypatch.setattr(network, "_coredevice_details_once", read)
    monkeypatch.setattr(network.WDAClient, "call", request)
    binding = network.NetworkBinding(UDID, f"http://[{IP}]:8100", timeout=0.01)
    with pytest.raises(network.NetworkBindingError) as caught:
        await network.BoundWDAClient(binding).call("POST", "/mutation", {})
    assert isinstance(caught.value, network.CoreDeviceTimeoutError)
    assert reads == [(UDID, 0.01), (UDID, 0.01)]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [network.NetworkBindingError("CoreDevice details failed with exit status 1"),
                                  network.NetworkBindingError("CoreDevice did not write valid device details"),
                                  asyncio.CancelledError()])
async def test_details_does_not_retry_other_failures(monkeypatch, error):
    reads = []

    async def read(udid, timeout):
        reads.append((udid, timeout))
        raise error

    monkeypatch.setattr(network, "_coredevice_details_once", read)
    with pytest.raises(type(error)) as caught:
        await network._coredevice_details(UDID, 0.01)
    assert caught.value is error
    assert len(reads) == 1


def ddi_error(*codes):
    """devicectl's nested NSError chain, outermost code first (shape from a locked iOS 26 phone)."""
    error = None
    for code in reversed(codes):
        info = {"NSLocalizedDescription": {"string": f"error {code}"}}
        if error is not None:
            info["NSUnderlyingError"] = {"error": error}
        error = {"code": code, "domain": "com.apple.dt.CoreDeviceError", "userInfo": info}
    return error


def fake_devicectl(monkeypatch, payload, status, commands):
    class Process:
        pid = 12345
        returncode = status

        async def wait(self):
            return status

    async def spawn(*command, **kwargs):
        commands.append(command)
        if payload is not None:
            Path(command[command.index("--json-output") + 1]).write_text(json.dumps(payload))
        return Process()

    monkeypatch.setattr(network.asyncio, "create_subprocess_exec", spawn)


async def test_developer_image_success_returns(monkeypatch):
    commands = []
    fake_devicectl(monkeypatch, {"info": {"outcome": "success"}, "result": {}}, 0, commands)
    await network.ensure_developer_image(UDID, 30)
    assert commands[0][:6] == ("xcrun", "devicectl", "device", "info", "ddiServices", "--device")
    assert commands[0][6] == UDID


async def test_locked_phone_is_a_recoverable_developer_image_error(monkeypatch):
    payload = {"info": {"outcome": "failed"}, "error": ddi_error(12040, 12040, -402652958, -402652958)}
    fake_devicectl(monkeypatch, payload, 1, [])
    with pytest.raises(network.DeveloperImageLockedError, match="unlock") as raised:
        await network.ensure_developer_image(UDID, 30)
    assert network.is_recoverable_error(raised.value)


@pytest.mark.parametrize("codes", [(3,), (12040, 12040, -402652907, -402652907)])
async def test_transient_developer_image_failures_are_recoverable_but_not_lock(monkeypatch, codes):
    # Observed over paired network: "Failed to acquire assertion" and 0xE8000115 property query failures.
    fake_devicectl(monkeypatch, {"info": {"outcome": "failed"}, "error": ddi_error(*codes)}, 1, [])
    with pytest.raises(network.DeveloperImageUnavailableError, match="Developer disk image is unavailable") as raised:
        await network.ensure_developer_image(UDID, 30)
    assert not isinstance(raised.value, network.DeveloperImageLockedError)
    assert network.is_recoverable_error(raised.value)


async def test_developer_image_error_names_each_nested_reason_once(monkeypatch):
    fake_devicectl(monkeypatch, {"info": {"outcome": "failed"}, "error": ddi_error(12040, 12040, -402652907)}, 1, [])
    with pytest.raises(network.DeveloperImageUnavailableError) as raised:
        await network.ensure_developer_image(UDID, 30)
    assert str(raised.value) == "Developer disk image is unavailable: error 12040; error -402652907"


@pytest.mark.parametrize("payload", [None, ["not", "an", "object"]])
async def test_unreadable_developer_image_result_is_fatal(monkeypatch, payload):
    fake_devicectl(monkeypatch, payload, 1, [])
    with pytest.raises(network.NetworkBindingError, match="Developer disk image") as raised:
        await network.ensure_developer_image(UDID, 30)
    assert not network.is_recoverable_error(raised.value)
