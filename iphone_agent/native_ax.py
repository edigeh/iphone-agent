from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
import plistlib
import re
import struct
from typing import Any

from pymobiledevice3.dtx.connection import DTXConnection
from pymobiledevice3.dtx.message import DTXMessage, DTXMessageType
from pymobiledevice3.dtx.structs import DtxFragmentHeader, FRAGMENT_HEADER_MIN_SIZE

AX_SERVICE = "com.apple.accessibility.axAuditDaemon.remoteAXService"
AX_INTERFACE_ORIENTATION = 129
ORIENTATION_NAMES = frozenset({"portrait", "portraitUpsideDown", "landscapeLeft", "landscapeRight"})
MAX_FRAME = 10_000_000
ROLES = {1: "Application", 2: "Button", 3: "Toggle", 6: "Heading", 7: "Image", 8: "Link", 9: "RadioButton", 13: "Adjustable", 14: "StaticText", 15: "TextField", 17: "TextArea", 19: "PopUpButton"}
SUBROLES = {0: "None", 1: "SearchField", 2: "TabButton", 3: "Switch", 4: "Map", 5: "SecureTextField"}


class AXProtocolError(RuntimeError):
    pass


def integer(value: Any, bits: int, *, unsigned=False, default=None) -> int:
    if value is None and default is not None:
        return default
    low, high = (0, 2**bits - 1) if unsigned else (-2**(bits-1), 2**(bits-1)-1)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise AXProtocolError(f"expected {'unsigned' if unsigned else 'signed'} {bits}-bit integer")
    return value


@dataclass(frozen=True)
class ElementRef:
    value: int

    def __post_init__(self):
        integer(self.value, 64, unsigned=True)


@dataclass(frozen=True)
class Response:
    kind: int = 0
    attribute: int = 0
    error: int = 0
    value: Any = None
    notification: Any = None
    element: ElementRef | None = None


class ArchiveDecoder:
    """Local class registry: never changes bpylist2's process-global adapters."""

    def decode(self, data: bytes) -> Any:
        if not isinstance(data, bytes) or len(data) > MAX_FRAME:
            raise AXProtocolError("nested AX archive exceeds frame limit or is not bytes")
        try:
            archive = plistlib.loads(data)
            if archive.get("$archiver") != "NSKeyedArchiver":
                raise AXProtocolError("expected NSKeyedArchiver")
            self.objects = archive["$objects"]
            if not isinstance(self.objects, list) or len(self.objects) > 100_000:
                raise AXProtocolError("archive object limit")
            self.cache, self.active = {}, set()
            return self.resolve(archive["$top"]["root"], 0)
        except AXProtocolError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError) as exc:
            raise AXProtocolError("malformed nested AX archive") from exc

    def resolve(self, value, depth):
        if depth > 256:
            raise AXProtocolError("archive depth limit")
        if isinstance(value, plistlib.UID):
            index = value.data
            if index == 0:
                return None
            if not 0 <= index < len(self.objects) or index in self.active:
                raise AXProtocolError("invalid or cyclic archive reference")
            if index in self.cache:
                return self.cache[index]
            self.active.add(index)
            result = self.resolve(self.objects[index], depth + 1)
            self.active.remove(index)
            self.cache[index] = result
            return result
        if isinstance(value, list):
            return [self.resolve(v, depth + 1) for v in value]
        if not isinstance(value, dict):
            return value
        if "$class" not in value:
            return {k: self.resolve(v, depth + 1) for k, v in value.items()}
        class_ref = value["$class"]
        if not isinstance(class_ref, plistlib.UID):
            raise AXProtocolError("invalid archive class")
        name = self.objects[class_ref.data]["$classname"]
        def get(key, default=None):
            return self.resolve(value.get(key, default), depth + 1)
        if name == "AXPTranslationObject":
            return ElementRef(integer(get("objectID"), 64, unsigned=True))
        if name == "AXPTranslatorResponse":
            element = get("associatedTranslationObject")
            if element is not None and not isinstance(element, ElementRef):
                raise AXProtocolError("response target must be an element reference")
            return Response(integer(get("associatedRequestType"), 32, default=0),
                            integer(get("attribute"), 32, default=0),
                            integer(get("error"), 64, default=0), get("resultData"),
                            get("notification"), element)
        if name in {"NSArray", "NSMutableArray", "NSSet", "NSMutableSet"}:
            return get("NS.objects", [])
        if name in {"NSDictionary", "NSMutableDictionary"}:
            keys, values = get("NS.keys", []), get("NS.objects", [])
            if not isinstance(keys, list) or not isinstance(values, list) or len(keys) != len(values):
                raise AXProtocolError("invalid archived dictionary")
            return dict(zip(keys, values))
        if name == "NSNull":
            return None
        if name in {"NSString", "NSMutableString"}:
            return get("NS.string")
        if name in {"NSData", "NSMutableData"}:
            return get("NS.data")
        if name == "NSNumber":
            for key in ("NS.boolval", "NS.intval", "NS.real", "NS.floatval", "NS.doubleval"):
                if key in value:
                    return get(key)
            raise AXProtocolError("unknown NSNumber representation")
        if name == "NSValue":
            for key in ("NS.rectval", "NS.pointval", "NS.sizeval"):
                if key in value:
                    return get(key)
            return {"location": get("NS.rangeval.location"), "length": get("NS.rangeval.length")}
        if name in {"NSAttributedString", "NSMutableAttributedString", "NSConcreteAttributedString", "NSConcreteMutableAttributedString"}:
            return get("NSString")
        if name == "NSURL":
            from urllib.parse import urljoin
            return urljoin(get("NS.base") or "", get("NS.relative") or "")
        if name == "NSError":
            return {"domain": get("NSDomain"), "code": get("NSCode"), "userInfo": get("NSUserInfo")}
        if name == "NSDate":
            return get("NS.time")
        if name == "NSUUID":
            return get("NS.uuidbytes")
        raise AXProtocolError(f"unsupported archive class {name!r}")


def rectangle(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*\{\s*\{\s*([^,]+),\s*([^}]+)\}\s*,\s*\{\s*([^,]+),\s*([^}]+)\}\s*\}\s*", value)
    if not match:
        return None
    try:
        x, y, width, height = map(float, match.groups())
    except ValueError:
        return None
    if not all(math.isfinite(v) for v in (x, y, width, height)) or width < 0 or height < 0:
        return None
    return {"left": x, "top": y, "right": x + width, "bottom": y + height}


@dataclass
class Element:
    attributes: dict = field(default_factory=dict)
    actions: list = field(default_factory=list)


class AXTree:
    def __init__(self, *, max_elements=100_000, max_nodes=20_000, max_depth=256):
        self.max_elements, self.max_nodes, self.max_depth = max_elements, max_nodes, max_depth
        self.elements: dict[int, Element] = {}
        self.root = 0
        self.generation = 0

    def apply(self, response: Response) -> bool:
        if not isinstance(response, Response):
            raise AXProtocolError("nested root must be AXPTranslatorResponse")
        if response.kind != 11:
            return False
        if response.error:
            raise AXProtocolError(f"AX tree request refused ({response.error})")
        value = response.value
        if not isinstance(value, dict):
            raise AXProtocolError("missing treeDump envelope")
        kind = value.get("treeDumpType")
        if kind not in (None, "AXPTreeDumpTypeInitialDump", "AXPTreeDumpTypeAdditionalData", "AXPTreeDumpTypeTreeDestroyed"):
            raise AXProtocolError("unknown tree dump type")
        if kind == "AXPTreeDumpTypeTreeDestroyed":
            self.elements, self.root = {}, 0
            self.generation += 1
            return True
        if kind is None and "treeDump" not in value:
            raise AXProtocolError("missing treeDump envelope")
        responses = value.get("treeDump")
        if responses is None:
            responses = []
        if not isinstance(responses, list) or not all(isinstance(r, Response) for r in responses):
            raise AXProtocolError("treeDump must be a response array")
        initial = kind == "AXPTreeDumpTypeInitialDump"
        elements = {} if initial else {k: Element(dict(v.attributes), list(v.actions)) for k, v in self.elements.items()}
        root = 0 if initial else self.root
        for r in responses:
            if r.error:
                continue
            if r.kind == 4:
                if not isinstance(r.value, ElementRef):
                    raise AXProtocolError("root response must contain element reference")
                if r.value.value:
                    root = r.value.value
                continue
            if r.kind not in (3, 5, 9):
                continue
            if not r.element or not r.element.value:
                continue
            item = elements.setdefault(r.element.value, Element())
            if len(elements) > self.max_elements:
                raise AXProtocolError("AX element limit")
            if r.kind == 5:
                if not isinstance(r.value, dict):
                    raise AXProtocolError("full attributes must be a dictionary")
                item.attributes = {integer(k, 32): v for k, v in r.value.items()}
            elif r.kind == 3:
                integer(r.attribute, 32)
                if r.value is None:
                    item.attributes.pop(r.attribute, None)
                else:
                    item.attributes[r.attribute] = r.value
            else:
                if not isinstance(r.value, list):
                    raise AXProtocolError("actions must be an array")
                item.actions = [integer(a, 64) for a in r.value]
        self.elements, self.root = elements, root
        self.generation += 1
        return True

    def snapshot(self):
        if self.root not in self.elements:
            raise AXProtocolError("AX snapshot has no populated root")
        # AX is a graph: a child of a secure field can also be reachable through
        # a nonsecure parent. Taint descendants by reference before rendering.
        protected = {ref for ref, item in self.elements.items()
                     if item.attributes.get(51) in (5, "SecureTextField", "AXSecureTextField")
                     or item.attributes.get(45) in ("SecureTextField", "AXSecureTextField")}
        pending = list(protected)
        while pending:
            item = self.elements.get(pending.pop())
            if item is None:
                continue
            children = item.attributes.get(8)
            if not isinstance(children, list) or not children:
                children = item.attributes.get(9, [])
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, ElementRef) and child.value not in protected:
                        protected.add(child.value)
                        pending.append(child.value)
        count, truncated = 0, False
        def walk(ref, path, depth, inherited_secure=False):
            nonlocal count, truncated
            if ref in path or depth > self.max_depth or count >= self.max_nodes:
                truncated = True
                return None
            element = self.elements.get(ref)
            if element is None:
                truncated = True
                return None
            count += 1
            a = element.attributes
            role = a.get(45) if isinstance(a.get(45), str) else ROLES.get(a.get(45)) if isinstance(a.get(45), int) else None
            subrole = a.get(51) if isinstance(a.get(51), str) else SUBROLES.get(a.get(51)) if isinstance(a.get(51), int) else None
            role = role.removeprefix("AX") if role else None
            subrole = subrole.removeprefix("AX") if subrole else None
            secure = inherited_secure or ref in protected
            label = a.get(33) if isinstance(a.get(33), str) else ""
            value = a.get(53)
            text = "••••" if secure else (value if isinstance(value, str) else label)
            node = {"elementId": str(ref), "resourceId": a.get(25) if not secure and isinstance(a.get(25), str) else "",
                    "className": "XCUIElementType" + (subrole if subrole and subrole != "None" else role or "Other"),
                    "text": text, "contentDescription": "" if secure else label, "password": secure,
                    "value": "••••" if secure else value if isinstance(value, (str, int, float, bool)) else None,
                    "enabled": bool(a.get(27, False)), "focused": bool(a.get(28, False)),
                    "selected": bool(a.get(30, False)), "visible": bool(a.get(32, False)),
                    "clickable": role in {"Button", "Toggle", "Link", "RadioButton", "PopUpButton"},
                    "children": []}
            bounds = rectangle(a.get(21))
            if bounds:
                node["boundsInScreen"] = bounds
            children = a.get(8)
            if not isinstance(children, list) or not children:
                children = a.get(9, [])
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, ElementRef):
                        rendered = walk(child.value, path | {ref}, depth + 1, secure)
                        if rendered:
                            node["children"].append(rendered)
            return node
        tree = walk(self.root, set(), 0)
        attributes = self.elements[self.root].attributes
        phone = {"packageName": attributes.get(6) if isinstance(attributes.get(6), str) else None}
        token = attributes.get(60)
        raw = token.get("ElementData") if isinstance(token, dict) else None
        if isinstance(raw, bytes) and len(raw) == 20 and raw[8:12] == b"\0" * 4 and struct.unpack_from("<I", raw, 12)[0] == 1:
            pid = struct.unpack_from("<I", raw)[0]
            if 0 < pid <= 0x7fffffff:
                phone["pid"] = pid
        orientation = attributes.get(AX_INTERFACE_ORIENTATION)
        if not isinstance(orientation, str) or orientation not in ORIENTATION_NAMES:
            orientation = None
        return {"phone_state": phone, "a11y_tree": tree, "truncated": truncated,
                "generation": self.generation, "interfaceOrientation": orientation}


class BoundedDTXReader:
    """Validate fragment sizes before the dependency allocates their bodies."""
    def __init__(self, reader):
        self.reader, self.remaining, self.extension = reader, 0, 0

    async def readexactly(self, count):
        if count > MAX_FRAME:
            raise AXProtocolError("DTX frame limit")
        data = await self.reader.readexactly(count)
        if self.extension:
            self.extension = 0
        elif self.remaining:
            self.remaining = 0
        else:
            if count != FRAGMENT_HEADER_MIN_SIZE:
                raise AXProtocolError("unexpected DTX fragment read")
            header = DtxFragmentHeader.parse(data)
            if header.data_size > MAX_FRAME or not FRAGMENT_HEADER_MIN_SIZE <= header.header_size <= 4096 or header.count > 256:
                raise AXProtocolError("DTX frame or fragment limit")
            self.extension = header.header_size - FRAGMENT_HEADER_MIN_SIZE
            self.remaining = 0 if header.index == 0 and header.count > 1 else header.data_size
        return data


class RemoteAXConnection(DTXConnection):
    """Surface ERROR replies to asynchronous refreshes instead of dropping them."""
    async def _process_message(self, raw_message, fragment):
        if (fragment.conversation_index and raw_message and
                raw_message[0] == DTXMessageType.ERROR and
                fragment.identifier not in self._pending_replies):
            message = DTXMessage.parse(fragment, raw_message)
            code = getattr(message.payload, "code", "unknown")
            error = AXProtocolError(f"asynchronous AX request refused ({code})")
            self.on_async_error(error)
            raise error
        return await super()._process_message(raw_message, fragment)


class NativeAX:
    def __init__(self, rsd, *, timeout=10.0):
        self.rsd, self.timeout = rsd, timeout
        self.tree = AXTree()
        self.connection = None
        self.changed = asyncio.Event()
        self.host_version_seen = asyncio.Event()
        self.failure = None
        self._snapshot_lock = asyncio.Lock()

    async def open(self):
        if self.connection is not None:
            return
        self.tree = AXTree()
        self.failure = None
        self.changed.clear()
        self.host_version_seen.clear()
        stream = await self.rsd.create_service_connection(self.rsd.get_service_port(AX_SERVICE))
        try:
            conn = RemoteAXConnection(BoundedDTXReader(stream.reader), stream.writer)
        except BaseException:
            await stream.close()
            raise
        self.connection = conn
        conn.sent_capabilities = None
        conn.on_async_error = self._failed
        self._early_messages = 0
        channel = conn._ctrl_channel
        channel._queue = asyncio.Queue(maxsize=256)
        original = channel.on_invoke
        async def invoke(selector, args):
            if selector == "hostAPIVersion":
                self.host_version_seen.set()
                return 7
            if not self.host_version_seen.is_set():
                self._early_messages += 1
                if self._early_messages > 256:
                    error = AXProtocolError("more than 256 early AX messages")
                    self._failed(error)
                    raise error
            if selector == "processDataFromRemoteDevice:":
                try:
                    if not args or not isinstance(args[0], bytes):
                        raise AXProtocolError("AX push requires nested archive bytes")
                    if self.tree.apply(ArchiveDecoder().decode(args[0])):
                        self.changed.set()
                except Exception as exc:
                    self._failed(exc)
                    raise
                return None
            return await original(selector, args) if original else None
        channel.on_invoke = invoke
        channel.on_closed = lambda reason: self._closed(reason)
        try:
            async with asyncio.timeout(self.timeout):
                await conn.connect()
                await channel.invoke("_notifyOfPublishedCapabilities:", {"com.apple.private.DTXConnection": 1}, expects_reply=False)
                version = await channel.invoke("deviceAPIVersion")
                self.device_version = integer(version, 64, unsigned=True)
                if not self.device_version:
                    raise AXProtocolError("device API version must be positive")
                await self.host_version_seen.wait()
                await channel.invoke("clientNeedsAccessibility:", True)
        except BaseException:
            await self.close()
            raise

    def _failed(self, error):
        self.failure = error
        self.changed.set()

    def _closed(self, reason):
        if self.failure is None:
            self.failure = AXProtocolError(f"AX channel closed: {reason}")
        self.changed.set()
        self.host_version_seen.set()

    async def snapshot(self):
        async with self._snapshot_lock:
            await self.open()
            baseline = self.tree.generation
            self.changed.clear()
            async with asyncio.timeout(self.timeout):
                await self.connection._ctrl_channel.invoke("clientNeedsAccessibility:", True, expects_reply=False)
                while True:
                    if self.failure:
                        raise self.failure
                    if self.tree.generation > baseline and self.tree.root in self.tree.elements:
                        return self.tree.snapshot()
                    await self.changed.wait()
                    self.changed.clear()

    async def close(self):
        conn, self.connection = self.connection, None
        self._closed("client closed")
        if conn:
            await conn.aclose()
