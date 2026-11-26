import asyncio
import plistlib
import struct
from types import SimpleNamespace

import pytest

import iphone_agent.native_ax as ax
from iphone_agent.native_ax import AXProtocolError, ArchiveDecoder, AXTree, ElementRef as Ref, Response as R


def dump(*responses, kind="AXPTreeDumpTypeInitialDump"):
    return R(11, value={"treeDumpType": kind, "treeDump": list(responses)})


def initial():
    return dump(R(4, value=Ref(1)), R(5, element=Ref(1), value={21: "{{0, 0}, {390, 844}}", 45: 1, 6: "example.fixture", 8: [Ref(2), 999]}),
                R(5, element=Ref(2), value={21: "{{20, 30}, {100, 40}}", 45: 15, 51: 5, 53: "private-secret", 33: "Password", 27: True, 32: True}))


def test_full_delta_destroyed_and_secure_values():
    tree = AXTree()
    tree.apply(initial())
    result = tree.snapshot()
    assert result["phone_state"]["packageName"] == "example.fixture"
    assert result["a11y_tree"]["boundsInScreen"] == {"left": 0, "top": 0, "right": 390, "bottom": 844}
    assert len(result["a11y_tree"]["children"]) == 1  # numeric attributes are not references
    assert "private-secret" not in str(result)
    assert result["a11y_tree"]["children"][0]["password"]
    tree.apply(dump(R(3, 53, value=None, element=Ref(2)), R(3, 33, error=12, value="ignored", element=Ref(2)), kind=None))
    assert 53 not in tree.elements[2].attributes
    assert tree.elements[2].attributes[33] == "Password"
    tree.apply(dump(R(5, value={25: "replacement"}, element=Ref(2)), kind="AXPTreeDumpTypeAdditionalData"))
    assert tree.elements[2].attributes == {25: "replacement"}
    tree.apply(R(11, value={"treeDumpType": "AXPTreeDumpTypeTreeDestroyed"}))
    assert tree.root == 0 and tree.elements == {} and tree.generation == 4
    with pytest.raises(AXProtocolError, match="populated root"):
        tree.snapshot()


def test_limits_cycle_root_only_and_atomic_invalid_delta():
    tree = AXTree(max_elements=2, max_nodes=2, max_depth=2)
    tree.apply(initial())
    before = tree.snapshot()
    with pytest.raises(AXProtocolError, match="element limit"):
        tree.apply(dump(R(5, value={}, element=Ref(3)), kind=None))
    assert tree.snapshot() == before
    with pytest.raises(AXProtocolError, match="32-bit"):
        tree.apply(dump(R(5, value={"21": "bad"}, element=Ref(1)), kind=None))
    assert tree.snapshot() == before
    tree.apply(dump(R(3, 8, value=[Ref(1)], element=Ref(2)), kind=None))
    assert tree.snapshot()["truncated"]
    tree.apply(dump(R(4, value=Ref(1))))
    with pytest.raises(AXProtocolError):
        tree.snapshot()


@pytest.mark.parametrize("value", [True, -1, 2**64, "1"])
def test_element_reference_uint64(value):
    with pytest.raises(AXProtocolError):
        Ref(value)


def archive_fixture():
    # Independent NSKeyedArchive fixture: translation object ID 42 and integer
    # dictionary key 21. UID indices are explicit, including NSValue rectval.
    u = plistlib.UID
    objects = ["$null",
        {"$class": u(2), "associatedRequestType": 11, "resultData": u(3)},
        {"$classname": "AXPTranslatorResponse", "$classes": ["AXPTranslatorResponse", "NSObject"]},
        {"$class": u(4), "NS.keys": [u(5), u(6)], "NS.objects": [u(7), u(8)]},
        {"$classname": "NSDictionary", "$classes": ["NSDictionary", "NSObject"]},
        "treeDumpType", "treeDump", "AXPTreeDumpTypeInitialDump",
        {"$class": u(9), "NS.objects": [u(10), u(12)]},
        {"$classname": "NSArray", "$classes": ["NSArray", "NSObject"]},
        {"$class": u(2), "associatedRequestType": 4, "resultData": u(11)},
        {"$class": u(13), "objectID": 42},
        {"$class": u(2), "associatedRequestType": 5, "associatedTranslationObject": u(11), "resultData": u(14)},
        {"$classname": "AXPTranslationObject", "$classes": ["AXPTranslationObject", "NSObject"]},
        {"$class": u(4), "NS.keys": [u(15)], "NS.objects": [u(16)]},
        21,
        {"$class": u(17), "NS.rectval": u(18)},
        {"$classname": "NSValue", "$classes": ["NSValue", "NSObject"]},
        "{{0, 0}, {402, 874}}"]
    data = plistlib.dumps({"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": u(1)}, "$objects": objects}, fmt=plistlib.FMT_BINARY)
    return data, objects


def test_nested_archive_exact_class_and_keys():
    data, objects = archive_fixture()
    u = plistlib.UID
    decoded = ArchiveDecoder().decode(data)
    assert decoded.kind == 11
    tree = AXTree()
    tree.apply(decoded)
    assert tree.root == 42
    assert tree.snapshot()["a11y_tree"]["boundsInScreen"]["bottom"] == 874
    objects[11]["objectID"] = -1
    with pytest.raises(AXProtocolError):
        ArchiveDecoder().decode(plistlib.dumps({"$archiver": "NSKeyedArchiver", "$top": {"root": u(1)}, "$objects": objects}, fmt=plistlib.FMT_BINARY))


def test_pid_validation_and_no_foreground_invention():
    tree = AXTree()
    token = struct.pack("<IIIII", 123, 99, 0, 1, 7)
    tree.apply(dump(R(4, value=Ref(1)), R(5, element=Ref(1), value={60: {"ElementData": token}})))
    assert tree.snapshot()["phone_state"] == {"packageName": None, "pid": 123}
    tree.apply(dump(R(3, 60, value={"ElementData": token[:8] + b"bad!" + token[12:]}, element=Ref(1)), kind=None))
    assert "pid" not in tree.snapshot()["phone_state"]


class FakeChannel:
    def __init__(self, events):
        self.events = events
        self.on_invoke = None
        self.on_closed = None

    async def invoke(self, method, *args, expects_reply=True):
        self.events.append((method, args, expects_reply))
        if method == "deviceAPIVersion":
            return 9


@pytest.mark.asyncio
async def test_raw_dtx_preinstalled_host_callback_and_exact_handshake(monkeypatch):
    events = []
    class Connection:
        def __init__(self, reader, writer):
            self._ctrl_channel = FakeChannel(events)
            self.sent_capabilities = "default"
        async def connect(self):
            assert self.sent_capabilities is None
            answer = await self._ctrl_channel.on_invoke("hostAPIVersion", [])
            assert answer == 7
            events.append("connected")
        async def aclose(self):
            events.append("closed")
    class RSD:
        def get_service_port(self, name):
            assert name == ax.AX_SERVICE
            return 4321
        async def create_service_connection(self, port):
            assert port == 4321
            return SimpleNamespace(reader=object(), writer=object())
    monkeypatch.setattr(ax, "RemoteAXConnection", Connection)
    client = ax.NativeAX(RSD())
    await client.open()
    assert events == ["connected", ("_notifyOfPublishedCapabilities:", ({"com.apple.private.DTXConnection": 1},), False),
                      ("deviceAPIVersion", (), True), ("clientNeedsAccessibility:", (True,), True)]
    await client.close()
    assert events[-1] == "closed"


@pytest.mark.asyncio
async def test_snapshot_requires_new_generation_and_cancellation_keeps_no_waiter():
    client = ax.NativeAX(None, timeout=.02)
    client.tree.apply(initial())
    client.connection = SimpleNamespace(_ctrl_channel=FakeChannel([]))
    with pytest.raises(TimeoutError):
        await client.snapshot()
    task = asyncio.create_task(client.snapshot())
    await asyncio.sleep(0)
    client.tree.apply(dump(R(3, 33, value="fresh", element=Ref(1)), kind=None))
    client.changed.set()
    assert (await task)["generation"] == 2
    task = asyncio.create_task(client.snapshot())
    await asyncio.sleep(0)
    client._closed("transport lost")
    with pytest.raises(AXProtocolError, match="transport lost"):
        await task


@pytest.mark.asyncio
async def test_fragment_rejects_oversized_body_before_read():
    from pymobiledevice3.dtx.structs import DtxFragmentHeader
    header = DtxFragmentHeader(header_size=ax.FRAGMENT_HEADER_MIN_SIZE, index=0, count=1,
                              data_size=ax.MAX_FRAME + 1, identifier=1, conversation_index=0,
                              channel_code=0, flags=0).build()
    class Reader:
        async def readexactly(self, count):
            assert count == len(header)
            return header
    with pytest.raises(AXProtocolError, match="limit"):
        await ax.BoundedDTXReader(Reader()).readexactly(len(header))

@pytest.mark.asyncio
async def test_real_dtx_wire_handshake_nested_push_and_refusal():
    """Both wire endpoints run the pinned codec; no device or RemoteXPC involved."""
    from pymobiledevice3.dtx.connection import DTXConnection
    from pymobiledevice3.dtx.ns_types import NSError
    received, peers, tasks = [], [], []
    ready = asyncio.Event()
    refuse = False

    async def serve(reader, writer):
        conn = DTXConnection(reader, writer)
        peers.append(conn)
        conn.sent_capabilities = None
        original = conn._ctrl_channel.on_invoke
        async def invoke(selector, args):
            received.append((selector, args))
            if selector == "deviceAPIVersion":
                return NSError(13, "AX", {}) if refuse else 9
            if selector == "clientNeedsAccessibility:":
                assert args == [True]
                await conn._ctrl_channel.invoke("processDataFromRemoteDevice:", archive_fixture()[0], expects_reply=True)
                return None
            return await original(selector, args)
        conn._ctrl_channel.on_invoke = invoke
        await conn.connect()
        assert await conn._ctrl_channel.invoke("hostAPIVersion") == 7
        ready.set()
        await conn.wait_disconnected()

    def accepted(reader, writer):
        tasks.append(asyncio.create_task(serve(reader, writer)))

    server = await asyncio.start_server(accepted, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    class RSD:
        def get_service_port(self, name):
            assert name == ax.AX_SERVICE
            return port
        async def create_service_connection(self, selected):
            reader, writer = await asyncio.open_connection("127.0.0.1", selected)
            return SimpleNamespace(reader=reader, writer=writer)
    client = ax.NativeAX(RSD(), timeout=1)
    try:
        await client.open()
        await ready.wait()
        snapshot = await client.snapshot()
        assert snapshot["a11y_tree"]["boundsInScreen"]["right"] == 402
        capabilities = [args for method, args in received if method == "_notifyOfPublishedCapabilities:"]
        assert capabilities == [[{"com.apple.private.DTXConnection": 1}]]
        client.changed.clear()
        await peers[0].send_reply_error(0, 9999, 1, NSError(44, "AX", {}))
        await asyncio.wait_for(client.changed.wait(), 1)
        assert isinstance(client.failure, AXProtocolError)
        assert "44" in str(client.failure)
        await client.close()
        refuse = True
        failed = ax.NativeAX(RSD(), timeout=1)
        with pytest.raises(Exception, match="13|AX"):
            await failed.open()
        assert failed.connection is None
    finally:
        await client.close()
        for peer in peers:
            await peer.aclose()
        server.close()
        await server.wait_closed()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_unknown_numeric_roles_do_not_break_rendering():
    tree = AXTree()
    tree.apply(dump(R(4, value=Ref(1)), R(5, element=Ref(1), value={45: 999, 51: 998})))
    assert tree.snapshot()["a11y_tree"]["className"] == "XCUIElementTypeOther"


@pytest.mark.asyncio
async def test_dtx_constructor_failure_closes_raw_stream(monkeypatch):
    events = []
    class Stream:
        reader = writer = object()
        async def close(self): events.append("closed")
    class RSD:
        def get_service_port(self, name): return 123
        async def create_service_connection(self, port): return Stream()
    def broken(*args): raise RuntimeError("constructor")
    monkeypatch.setattr(ax, "RemoteAXConnection", broken)
    with pytest.raises(RuntimeError, match="constructor"):
        await ax.NativeAX(RSD()).open()
    assert events == ["closed"]

@pytest.mark.parametrize("with_value", [False, True])
def test_secure_description_and_descendants_never_expose_content(with_value):
    secret = "actual-secret"
    attributes = {45: 15, 51: 5, 33: secret, 25: secret, 8: [Ref(3)]}
    if with_value:
        attributes[53] = secret
    tree = AXTree()
    tree.apply(dump(R(4, value=Ref(1)),
                    R(5, element=Ref(1), value={8: [Ref(2), Ref(3)]}),
                    R(5, element=Ref(2), value=attributes),
                    R(5, element=Ref(3), value={45: 14, 33: secret, 53: secret, 25: secret})))
    assert secret not in str(tree.snapshot())


def test_action_ids_preserve_signed_int64_range_atomically():
    tree = AXTree()
    tree.apply(initial())
    actions = [-(2**63), 2**40, 2**63 - 1]
    tree.apply(dump(R(9, element=Ref(2), value=actions), kind=None))
    assert tree.elements[2].actions == actions
    with pytest.raises(AXProtocolError, match="64-bit"):
        tree.apply(dump(R(9, element=Ref(2), value=[2**63]), kind=None))
    assert tree.elements[2].actions == actions


@pytest.mark.parametrize("orientation,expected", [("portrait", "portrait"),
    ("landscapeRight", "landscapeRight"), (1, None), (True, None), ("faceUp", None)])
def test_snapshot_carries_only_named_interface_orientation(orientation, expected):
    tree = AXTree()
    tree.apply(dump(R(4, value=Ref(1)), R(5, element=Ref(1), value={129: orientation})))
    state = tree.snapshot()
    assert state["interfaceOrientation"] == expected and state["generation"] == 1
