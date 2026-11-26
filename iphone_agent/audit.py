"""Append-only request journal. Hashes detect accidental modification; they are not signatures."""
import hashlib
import json
import os
import time
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      default=_encode).encode()


def _encode(value):
    from datetime import datetime
    import base64
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode()}
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def json_safe(value):
    return json.loads(canonical(value))


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class AuditLogBusy(BlockingIOError):
    pass


ROTATE_BYTES = 8 * 1024 * 1024


class AuditLog:
    """One writer per journal. A full or unreadable journal is set aside under a dated name and a new chain starts."""

    def __init__(self, path):
        import fcntl
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.set_aside = None
        while True:
            self.fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    current = os.stat(self.path).st_ino
                except FileNotFoundError:
                    current = None
                if current != os.fstat(self.fd).st_ino:
                    # Another controller set this file aside between our open and our lock.
                    os.close(self.fd)
                    continue
                reason = None
                try:
                    records = self.verify(self.path)
                    if os.fstat(self.fd).st_size > ROTATE_BYTES:
                        reason = "full"
                except (ValueError, KeyError, TypeError):
                    # A writer killed mid-record leaves a torn line. Failing here would block every
                    # later session, so the journal is kept as it is under a name that says so.
                    reason = "damaged"
                if reason is None:
                    self.last = records[-1]["hash"] if records else "0" * 64
                    return
                self.set_aside = self.path.with_name(
                    f"{self.path.stem}-{reason}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}{self.path.suffix}")
                os.replace(self.path, self.set_aside)
                os.close(self.fd)
            except BlockingIOError as exc:
                os.close(self.fd)
                raise AuditLogBusy(f"Another iphone-agent controller (serve, or an MCP session of an older version) "
                                   f"is using {self.path}. Stop it, then retry the call.") from exc
            except BaseException:
                os.close(self.fd)
                raise

    @staticmethod
    def verify(path):
        previous, records = "0" * 64, []
        if not Path(path).exists():
            return records
        for line in Path(path).read_text().splitlines():
            record = json.loads(line)
            expected = record.pop("hash")
            if record["previous"] != previous or digest(record) != expected:
                raise ValueError("Audit chain is damaged")
            record["hash"] = expected
            records.append(record)
            previous = expected
        return records

    def append(self, event, **fields):
        record = {"time_ns": time.time_ns(), "event": event, "previous": self.last, **fields}
        record["hash"] = digest(record)
        payload = canonical(record) + b"\n"
        # Lock is held for the journal lifetime; no other controller may append concurrently.
        view = memoryview(payload)
        while view:
            count = os.write(self.fd, view)
            if count <= 0:
                raise OSError("Audit write failed")
            view = view[count:]
        os.fsync(self.fd)
        self.last = record["hash"]
        return record

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
