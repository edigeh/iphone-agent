"""Opt-in, read-only Messages snapshots from a USB MobileBackup2 backup.

Only the HomeDomain SMS database is retained. No sending or restore path exists.
``refresh`` runs a device backup only when called by the operator; queries read
the last completed local snapshot and never touch the phone.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import os
import plistlib
import re
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.mobilebackup2 import BackupSelection, Mobilebackup2Service
from pymobiledevice3.usbmux import list_devices


class SMSUnsupported(RuntimeError):
    pass


class MessageStore:
    def __init__(self, udid: str, backup_dir: str | Path):
        if not isinstance(udid, str) or not re.fullmatch(r"[A-Za-z0-9-]+", udid):
            raise ValueError("a valid, explicit device UDID is required")
        self.udid = udid
        self.backup_dir = Path(backup_dir).expanduser()
        self._refresh_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None

    @property
    def device_dir(self) -> Path:
        return self.backup_dir / self.udid

    @property
    def _dirty_path(self) -> Path:
        return self.device_dir / "sms-refresh-incomplete"

    def _has_dirty_marker(self) -> bool:
        try:
            status = self._dirty_path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode) & 0o077:
            raise PermissionError("SMS refresh marker must be a private regular file (mode 0600)")
        return True

    def _sync_device_directory(self) -> None:
        directory = os.open(self.device_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _mark_refresh_dirty(self) -> None:
        self._has_dirty_marker()  # Reject unsafe existing paths before opening.
        fd = os.open(self._dirty_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode) & 0o077:
                raise PermissionError("SMS refresh marker must be a private regular file (mode 0600)")
            os.write(fd, b"incomplete\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        self._sync_device_directory()

    def _clear_refresh_dirty(self) -> None:
        if not self._has_dirty_marker():
            raise RuntimeError("SMS refresh marker disappeared during backup")
        self._dirty_path.unlink()
        self._sync_device_directory()

    async def refresh(self) -> dict[str, Any]:
        """Run an incremental USB backup retaining only the SMS database."""
        async with self._refresh_lock:
            self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if stat.S_IMODE(self.backup_dir.stat().st_mode) & 0o077:
                raise PermissionError("SMS backup directory must be private (mode 0700)")
            self.device_dir.mkdir(exist_ok=True, mode=0o700)
            if stat.S_IMODE(self.device_dir.stat().st_mode) & 0o077:
                raise PermissionError("per-device SMS backup directory must be private (mode 0700)")

            devices = await list_devices()
            matches = [d for d in devices if d.connection_type == "USB" and d.matches_udid(self.udid)]
            if len(matches) != 1:
                raise RuntimeError(f"expected one USB device with UDID {self.udid}; found {len(matches)}")
            lockdown = await create_using_usbmux(
                serial=matches[0].serial, connection_type="USB", autopair=False,
                label="Local iPhone Messages Backup",
            )
            try:
                if not lockdown.paired:
                    raise RuntimeError("the selected USB iPhone is not paired with this Mac")
                async with Mobilebackup2Service(lockdown) as backup:
                    if await backup.get_will_encrypt():
                        raise SMSUnsupported("encrypted device backups require a separately configured password flow")
                    selection = Mobilebackup2Service.selection_filter_callback(BackupSelection.SMS.rules())
                    # A failed incremental backup can alter the local snapshot.
                    # Persist this marker before that effect and fail closed on
                    # reads until backup, indexing and count all finish.
                    self._mark_refresh_dirty()
                    await backup.backup(
                        full=False, backup_directory=self.backup_dir,
                        filter_callback=selection, patch_manifest=False, unback=False,
                    )
            finally:
                await lockdown.close()
            await asyncio.to_thread(self._record_seen_sync, allow_dirty=True)
            count = await asyncio.to_thread(self._count_sync, allow_dirty=True)
            self._clear_refresh_dirty()
            return {"messages": count}

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"SMS snapshot database is missing: {path}")
        uri = "file:" + quote(str(path.absolute()), safe="/") + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def _sms_db_path(self, *, allow_dirty: bool = False) -> Path:
        if not allow_dirty and self._has_dirty_marker():
            raise SMSUnsupported("SMS snapshot refresh is incomplete; run sms_refresh to recover")
        manifest_path = self.device_dir / "Manifest.db"
        with closing(self._connect(manifest_path)) as manifest:
            row = manifest.execute(
                "SELECT fileID FROM Files WHERE domain = ? AND relativePath = ? LIMIT 1",
                ("HomeDomain", "Library/SMS/sms.db"),
            ).fetchone()
        if row is None:
            raise FileNotFoundError("HomeDomain/Library/SMS/sms.db is absent from this backup")
        file_id = row["fileID"]
        if not isinstance(file_id, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", file_id):
            raise ValueError("Manifest.db contains an invalid SMS fileID")
        nested = self.device_dir / file_id[:2] / file_id
        flat = self.device_dir / file_id
        for candidate in (nested, flat):
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        raise FileNotFoundError("Manifest.db names an SMS database that the backup did not retain")

    def _count_sync(self, *, allow_dirty: bool = False) -> int:
        with closing(self._connect(self._sms_db_path(allow_dirty=allow_dirty))) as database:
            return int(database.execute("SELECT COUNT(*) FROM message").fetchone()[0])

    @property
    def _seen_path(self) -> Path:
        return self.device_dir / "sms-first-seen.sqlite3"

    def _seen_exists(self) -> bool:
        path = self._seen_path
        if path.is_symlink():
            raise PermissionError("SMS first-seen index must not be a symlink")
        if not path.exists():
            return False
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise PermissionError("SMS first-seen index must be a private file (mode 0600)")
        return True

    def _record_seen_sync(self, *, allow_dirty: bool = False) -> None:
        """Record when a successfully backed-up GUID first appeared locally."""
        with closing(self._connect(self._sms_db_path(allow_dirty=allow_dirty))) as database:
            guids = (database.execute("SELECT guid FROM message WHERE guid IS NOT NULL AND guid != ''")
                     if "guid" in self._columns(database, "message") else ())
            if not self._seen_exists():
                try:
                    fd = os.open(self._seen_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    self._seen_exists()
                else:
                    os.close(fd)
            observed_ms = time.time_ns() // 1_000_000
            with closing(sqlite3.connect(self._seen_path)) as index:
                with index:
                    index.execute("CREATE TABLE IF NOT EXISTS seen (guid TEXT PRIMARY KEY, stored_at_ms INTEGER NOT NULL)")
                    index.execute("CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY CHECK (id = 1), generation INTEGER NOT NULL)")
                    index.execute("INSERT OR IGNORE INTO meta (id, generation) VALUES (1, 0)")
                    index.executemany(
                        "INSERT OR IGNORE INTO seen (guid, stored_at_ms) VALUES (?, ?)",
                        ((row["guid"], observed_ms) for row in guids),
                    )
                    index.execute("UPDATE meta SET generation = generation + 1 WHERE id = 1")

    def _attach_seen(self, database: sqlite3.Connection) -> bool:
        if not self._seen_exists():
            return False
        uri = "file:" + quote(str(self._seen_path.absolute()), safe="/") + "?mode=ro"
        database.execute("ATTACH DATABASE ? AS seen_index", (uri,))
        return True

    @staticmethod
    def _timestamp_expression() -> str:
        return "CAST(CASE WHEN ABS(m.date) > 1000000000000 THEN m.date / 1000000 ELSE m.date * 1000 END + 978307200000 AS INTEGER)"

    @staticmethod
    def _filter_digest(*, direction: str, before: int | None, after: int | None,
                       address: str | None, thread: str | None, services: list[str],
                       stored_from: int | None, kinds: list[str] | None = None,
                       junk: str = "include") -> str:
        filters = {"direction": direction, "before": before, "after": after,
                   "address": address, "thread": thread, "services": sorted(set(services)),
                   "storedFrom": stored_from, "kinds": sorted(set(kinds or ["message"])),
                   "junk": junk}
        return hashlib.sha256(json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _generation(database: sqlite3.Connection, has_seen: bool) -> int:
        if not has_seen or database.execute(
            "SELECT 1 FROM seen_index.sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone() is None:
            return 0
        row = database.execute("SELECT generation FROM seen_index.meta WHERE id = 1").fetchone()
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _encode_cursor(order: str, date: int, message_id: int, ceiling: int,
                       generation: int, filter_digest: str) -> str:
        payload = json.dumps({"v": 2, "o": order, "d": date, "i": message_id,
                              "c": ceiling, "g": generation, "f": filter_digest}, separators=(",", ":"))
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str, order: str, filter_digest: str) -> dict[str, Any]:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
            raise ValueError("cursor must be a valid SMS pagination token")
        try:
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            payload = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("cursor must be a valid SMS pagination token") from exc
        if (not isinstance(payload, dict) or set(payload) != {"v", "o", "d", "i", "c", "g", "f"}
                or type(payload["v"]) is not int or payload["v"] != 2
                or payload["o"] != order
                or payload["f"] != filter_digest
                or any(type(payload[key]) is not int or payload[key] < 0 for key in ("d", "i", "c", "g"))
                or payload["i"] > payload["c"]):
            raise ValueError("cursor does not match this SMS order, filters or version")
        return payload

    @staticmethod
    def _columns(database: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in database.execute(f"PRAGMA table_info({table})")}

    @classmethod
    def _classification_expressions(cls, database: sqlite3.Connection,
                                    columns: set[str]) -> tuple[str, str, str]:
        """Apply the iOS database's optional-column fallbacks."""
        def numeric(name: str) -> str:
            return f"COALESCE(m.{name}, 0)" if name in columns else "0"

        associated = numeric("associated_message_type")
        kind = (f"CASE WHEN {associated} BETWEEN 1000 AND 4000 THEN 'reaction' "
                f"WHEN {numeric('item_type')} != 0 THEN 'system' ELSE 'message' END")
        chat_junk, chat_unknown = "0", "0"
        chats = cls._columns(database, "chat")
        joins = cls._columns(database, "chat_message_join")
        if "is_filtered" in chats and joins >= {"chat_id", "message_id"}:
            root = ("EXISTS (SELECT 1 FROM chat_message_join j JOIN chat c ON c.ROWID = j.chat_id "
                    "WHERE j.message_id = m.ROWID AND ")
            predicate = "COALESCE(c.is_filtered, 0) = 2"
            if "is_blackholed" in chats:
                predicate += " OR COALESCE(c.is_blackholed, 0) != 0"
            chat_junk = root + "(" + predicate + "))"
            chat_unknown = root + "COALESCE(c.is_filtered, 0) = 1)"
        junk = f"({numeric('is_spam')} != 0 OR {numeric('filter_action')} = 2 OR {chat_junk})"
        unknown = f"({chat_unknown} AND NOT {junk})"
        return kind, junk, unknown

    @classmethod
    def _query_parts(cls, database: sqlite3.Connection) -> tuple[str, str, str, set[str]]:
        columns = cls._columns(database, "message")
        if not columns:
            raise ValueError("SMS snapshot lacks a message table")
        def field(name: str) -> str:
            return f"m.{name}" if name in columns else f"NULL AS {name}"

        handle_join = ""
        handle_field = "NULL AS address"
        if "handle_id" in columns and "id" in cls._columns(database, "handle"):
            handle_join = " LEFT JOIN handle h ON h.ROWID = m.handle_id"
            handle_field = "h.id AS address"

        chat_columns = cls._columns(database, "chat")
        thread_field = "NULL AS thread"
        if "guid" in chat_columns and cls._columns(database, "chat_message_join") >= {"chat_id", "message_id"}:
            thread_field = "(SELECT c.guid FROM chat_message_join cmj JOIN chat c ON c.ROWID = cmj.chat_id WHERE cmj.message_id = m.ROWID LIMIT 1) AS thread"

        date_field = field("date")
        if "date" in columns:
            timestamp_field = cls._timestamp_expression() + " AS timestamp_ms"
        else:
            timestamp_field = "NULL AS timestamp_ms"
        fields = ", ".join(["m.ROWID AS id", field("guid"), field("text"), field("subject"),
                            field("service"), date_field, timestamp_field,
                            field("is_from_me"), handle_field, thread_field])
        kind, junk, unknown = cls._classification_expressions(database, columns)
        fields += f", {kind} AS kind, {junk} AS is_junk, {unknown} AS is_unknown_sender"
        return fields, handle_join, timestamp_field, columns

    @staticmethod
    def _message(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "guid": row["guid"], "text": row["text"],
            "subject": row["subject"], "service": row["service"],
            "date": row["date"], "timestamp_ms": row["timestamp_ms"],
            "stored_at_ms": row["stored_at_ms"] if "stored_at_ms" in row.keys() else None,
            "direction": ("outgoing" if row["is_from_me"] else "incoming") if row["is_from_me"] is not None else None,
            "address": row["address"], "thread": row["thread"],
            "kind": row["kind"], "junk": bool(row["is_junk"]),
            "unknown_sender": bool(row["is_unknown_sender"]),
        }

    def _list_sync(
        self, *, direction: str, limit: int, before: int | None, after: int | None,
        address: str | None, thread: str | None, services: list[str],
        kinds: list[str], junk: str,
        stored_from: int | None, order: str, filter_digest: str,
        cursor: dict[str, Any] | None,
    ) -> dict[str, Any]:
        with closing(self._connect(self._sms_db_path())) as database:
            fields, joins, _timestamp_field, columns = self._query_parts(database)
            if "date" not in columns:
                raise SMSUnsupported("this SMS database has no message dates for stable ordering")
            has_seen = self._attach_seen(database)
            generation = self._generation(database, has_seen)
            if cursor is not None and cursor["g"] != generation:
                raise ValueError("cursor is stale after an SMS refresh")
            if has_seen and "guid" in columns:
                joins += " LEFT JOIN seen_index.seen s ON s.guid = m.guid"
                fields += ", s.stored_at_ms AS stored_at_ms"
            else:
                fields += ", NULL AS stored_at_ms"
            if stored_from is not None and (not has_seen or "guid" not in columns):
                raise SMSUnsupported("storedFrom requires a completed, indexed SMS refresh")
            if stored_from is not None and database.execute(
                "SELECT 1 FROM message WHERE guid IS NULL OR guid = '' LIMIT 1"
            ).fetchone() is not None:
                raise SMSUnsupported("storedFrom requires stable message GUIDs")
            if stored_from is not None and database.execute(
                "SELECT 1 FROM message m LEFT JOIN seen_index.seen s ON s.guid = m.guid "
                "WHERE s.guid IS NULL LIMIT 1"
            ).fetchone() is not None:
                raise SMSUnsupported("storedFrom requires a completed, indexed SMS refresh")
            conditions = []
            args: list[Any] = []
            kind_expression, junk_expression, unknown_expression = self._classification_expressions(database, columns)
            conditions.append(f"({kind_expression}) IN (" + ",".join("?" for _ in kinds) + ")")
            args.extend(kinds)
            if junk == "only":
                conditions.append(junk_expression)
            elif junk == "exclude":
                conditions.append("NOT " + junk_expression)
            elif junk == "unknownSender":
                conditions.append(unknown_expression)
            if direction != "all":
                if "is_from_me" not in columns:
                    raise SMSUnsupported("this SMS database cannot filter message direction")
                conditions.append("m.is_from_me = ?")
                args.append(1 if direction == "outgoing" else 0)
            if address is not None:
                if not joins:
                    raise SMSUnsupported("this SMS database has no handle addresses")
                conditions.append("h.id = ?")
                args.append(address)
            if thread is not None:
                if not self._columns(database, "chat_message_join") or not self._columns(database, "chat"):
                    raise SMSUnsupported("this SMS database has no chat joins")
                conditions.append("EXISTS (SELECT 1 FROM chat_message_join cmj JOIN chat c ON c.ROWID = cmj.chat_id WHERE cmj.message_id = m.ROWID AND c.guid = ?)")
                args.append(thread)
            if services:
                if "service" not in columns:
                    raise SMSUnsupported("this SMS database has no message services")
                conditions.append("m.service COLLATE BINARY IN (" + ",".join("?" for _ in services) + ")")
                args.extend(services)
            if stored_from is not None:
                conditions.append("s.stored_at_ms >= ?")
                args.append(stored_from)
            date_expression = self._timestamp_expression()
            expression = "COALESCE(" + date_expression + ", 0)"
            ceiling = cursor["c"] if cursor is not None else int(database.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0])
            conditions.append("m.ROWID <= ?")
            args.append(ceiling)
            if cursor is not None:
                comparison = ">" if order == "asc" else "<"
                conditions.append(f"({expression}, m.ROWID) {comparison} (?, ?)")
                args.extend((cursor["d"], cursor["i"]))
            if before is not None or after is not None:
                if before is not None:
                    conditions.append(date_expression + " < ?")
                    args.append(before)
                if after is not None:
                    conditions.append(date_expression + " > ?")
                    args.append(after)
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            direction_sql = "ASC" if order == "asc" else "DESC"
            rows = database.execute(
                f"SELECT {fields} FROM message m{joins}{where} ORDER BY {expression} {direction_sql}, m.ROWID {direction_sql} LIMIT ?",
                (*args, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        items = [self._message(row) for row in rows[:limit]]
        next_cursor = None
        if has_more:
            last = items[-1]
            next_cursor = self._encode_cursor(order, last["timestamp_ms"] or 0,
                                              last["id"], ceiling, generation, filter_digest)
        return {"messages": items, "next_cursor": next_cursor}

    async def list(
        self, *, direction: str = "all", limit: int = 50,
        before: int | None = None, after: int | None = None,
        address: str | None = None, thread: str | None = None,
        services: list[str] | None = None, stored_from: int | None = None,
        kinds: list[str] | None = None, junk: str = "include",
        order: str = "desc", cursor: str | None = None,
    ) -> dict[str, Any]:
        """List the last completed snapshot by message date and ROWID.

        Time bounds use UTC Unix milliseconds. Cursors are opaque and carry an
        exclusive date/ROWID position plus the initial snapshot's ROWID ceiling.
        """
        if direction not in {"all", "incoming", "outgoing"}:
            raise ValueError("direction must be all, incoming or outgoing")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")
        if order not in {"asc", "ascending", "desc", "descending"}:
            raise ValueError("order must be asc or desc")
        order = "asc" if order.startswith("asc") else "desc"
        if services is None:
            services = []
        if not isinstance(services, list) or any(not isinstance(value, str) or not value for value in services):
            raise ValueError("services must be a list of nonempty strings")
        if kinds is None or kinds == []:
            kinds = ["message"]
        if not isinstance(kinds, list) or any(not isinstance(kind, str) or kind not in {"message", "reaction", "system"} for kind in kinds):
            raise ValueError("kinds must be a list containing message, reaction or system")
        kinds = sorted(set(kinds))
        if junk is None or junk == "":
            junk = "include"
        if not isinstance(junk, str) or junk not in {"include", "exclude", "only", "unknownSender"}:
            raise ValueError("junk must be include, exclude, only or unknownSender")
        for name, value in (("address", address), ("thread", thread)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
        for name, value in (("before", before), ("after", after), ("stored_from", stored_from)):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer")
        filter_digest = self._filter_digest(direction=direction, before=before, after=after,
                                            address=address, thread=thread, services=services,
                                            stored_from=stored_from, kinds=kinds, junk=junk)
        decoded = self._decode_cursor(cursor, order, filter_digest) if cursor is not None else None
        async with self._refresh_lock:
            return await asyncio.to_thread(
                self._list_sync, direction=direction, limit=limit, before=before,
                after=after, address=address, thread=thread, services=services,
                kinds=kinds, junk=junk,
                stored_from=stored_from, order=order, filter_digest=filter_digest,
                cursor=decoded,
            )

    def _get_sync(self, message_id: int | str) -> dict[str, Any] | None:
        with closing(self._connect(self._sms_db_path())) as database:
            fields, joins, _, columns = self._query_parts(database)
            if self._attach_seen(database) and "guid" in columns:
                joins += " LEFT JOIN seen_index.seen s ON s.guid = m.guid"
                fields += ", s.stored_at_ms AS stored_at_ms"
            else:
                fields += ", NULL AS stored_at_ms"
            if isinstance(message_id, str) and not message_id.isdecimal():
                condition = "m.guid = ?"
            else:
                condition = "m.ROWID = ?"
            row = database.execute(
                f"SELECT {fields} FROM message m{joins} WHERE {condition} LIMIT 1", (message_id,)
            ).fetchone()
        return None if row is None else self._message(row)

    async def get(self, message_id: int | str) -> dict[str, Any] | None:
        if not isinstance(message_id, (int, str)) or (isinstance(message_id, int) and message_id < 0) or not str(message_id):
            raise ValueError("message_id must be a message GUID or nonnegative ROWID")
        async with self._refresh_lock:
            return await asyncio.to_thread(self._get_sync, message_id)

    def _subscriptions_sync(self) -> dict[str, Any]:
        if self._has_dirty_marker():
            raise SMSUnsupported("SMS snapshot refresh is incomplete; run sms_refresh to recover")
        for directory in (self.backup_dir, self.device_dir):
            if not directory.exists() and not directory.is_symlink():
                raise SMSUnsupported("SIM subscriptions require phone-number metadata in the completed backup")
            if directory.is_symlink() or not directory.is_dir() or stat.S_IMODE(directory.stat().st_mode) & 0o077:
                raise PermissionError("SMS backup directories must be private (mode 0700)")
        path = self.device_dir / "Info.plist"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError as exc:
            raise SMSUnsupported("SIM subscriptions require phone-number metadata in the completed backup") from exc
        try:
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode) or status.st_size > 1024 * 1024:
                raise ValueError("Invalid backup Info.plist metadata")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                try:
                    info = plistlib.load(stream)
                except (ValueError, plistlib.InvalidFileException) as exc:
                    raise ValueError("Invalid backup Info.plist metadata") from exc
        finally:
            os.close(fd)
        if not isinstance(info, dict):
            raise ValueError("Invalid backup Info.plist metadata")
        identity = info.get("Target Identifier", info.get("Unique Identifier"))
        if not isinstance(identity, str) or identity.casefold() != self.udid.casefold():
            raise ValueError("Backup Info.plist device identifier does not match the selected phone")
        number = info.get("Phone Number")
        if not isinstance(number, str):
            raise SMSUnsupported("SIM subscriptions require phone-number metadata in the completed backup")
        number = number.strip()
        if not re.fullmatch(r"(?:\+|00)[0-9 ().-]+", number):
            raise SMSUnsupported("Backup phone number has no unambiguous international format")
        digits = re.sub(r"[ ().-]", "", number)
        digits = digits[1:] if digits.startswith("+") else digits[2:]
        if not re.fullmatch(r"[1-9][0-9]{6,14}", digits):
            raise SMSUnsupported("Backup phone number has no unambiguous international format")
        # The fallback exposes only the normalized receiving number;
        # absent SIM IDs, slot, carrier and display name remain omitted.
        return {"subscriptions": [{"phoneNumber": "+" + digits}]}

    async def subscriptions(self) -> dict[str, Any]:
        async with self._refresh_lock:
            return await asyncio.to_thread(self._subscriptions_sync)

    @staticmethod
    def _protocol_message(message: dict[str, Any]) -> dict[str, Any]:
        """Expose known SMSMessage fields using the RPC camelCase spelling."""
        result = {
            "id": message["guid"] or str(message["id"]),
            "direction": message["direction"],
            "address": message["address"] or "",
            "body": message["text"] or "",
            "bodyAvailable": message["text"] is not None,
            "date": message["timestamp_ms"],
            "kind": message["kind"], "junk": message["junk"],
            "unknownSender": message["unknown_sender"],
        }
        for source, target in (("thread", "threadId"), ("subject", "subject"), ("service", "service")):
            if message[source] is not None:
                result[target] = message[source]
        if message["stored_at_ms"] is not None:
            result["storedAt"] = message["stored_at_ms"]
        return result

    def _max_id_sync(self) -> int:
        with closing(self._connect(self._sms_db_path())) as database:
            return int(database.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0])

    def _new_sync(self, last_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        with closing(self._connect(self._sms_db_path())) as database:
            fields, joins, _, columns = self._query_parts(database)
            if self._attach_seen(database) and "guid" in columns:
                joins += " LEFT JOIN seen_index.seen s ON s.guid = m.guid"
                fields += ", s.stored_at_ms AS stored_at_ms"
            else:
                fields += ", NULL AS stored_at_ms"
            rows = database.execute(
                f"SELECT {fields} FROM message m{joins} WHERE m.ROWID > ? ORDER BY m.ROWID ASC"
                + (" LIMIT ?" if limit is not None else ""),
                (last_id, limit) if limit is not None else (last_id,),
            ).fetchall()
            return [self._message(row) for row in rows]

    def _notification_cursor(self, message_id: int, guid: str | None) -> str:
        payload = {"v": 1, "u": hashlib.sha256(self.udid.encode()).hexdigest(),
                   "i": message_id, "g": guid}
        return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")

    def _read_notification_cursor(self, cursor: str) -> tuple[int, str | None]:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
            raise ValueError("cursor must be an SMS notification token")
        try:
            payload = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4),
                                                altchars=b"-_", validate=True))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("cursor must be an SMS notification token") from exc
        if (not isinstance(payload, dict) or set(payload) != {"v", "u", "i", "g"}
                or type(payload["v"]) is not int or payload["v"] != 1
                or payload["u"] != hashlib.sha256(self.udid.encode()).hexdigest()
                or type(payload["i"]) is not int or payload["i"] < 0
                or (payload["i"] == 0 and payload["g"] is not None)
                or (payload["i"] > 0 and (not isinstance(payload["g"], str) or not payload["g"]))):
            raise ValueError("cursor does not match this phone or SMS notification version")
        return payload["i"], payload["g"]

    def _notification_baseline_sync(self) -> tuple[int, str | None]:
        with closing(self._connect(self._sms_db_path())) as database:
            if "guid" not in self._columns(database, "message"):
                raise SMSUnsupported("SMS notifications require stable message GUIDs")
            row = database.execute("SELECT ROWID, guid FROM message ORDER BY ROWID DESC LIMIT 1").fetchone()
            if row is None:
                return 0, None
            if not isinstance(row["guid"], str) or not row["guid"]:
                raise SMSUnsupported("SMS notifications require stable message GUIDs")
            return row["ROWID"], row["guid"]

    def _notification_batch_sync(self, last_id: int, guid: str | None,
                                 limit: int) -> list[dict[str, Any]]:
        if last_id:
            with closing(self._connect(self._sms_db_path())) as database:
                row = database.execute("SELECT guid FROM message WHERE ROWID = ?", (last_id,)).fetchone()
                if row is None or row["guid"] != guid:
                    raise ValueError("SMS notification cursor is stale; start a new subscription")
        messages = self._new_sync(last_id, limit)
        if any(not isinstance(message["guid"], str) or not message["guid"] for message in messages):
            raise SMSUnsupported("SMS notifications require stable message GUIDs")
        return messages

    async def wait_for_messages(self, *, cursor: str | None = None, timeout: float = 90,
                                interval: int = 60, limit: int = 200) -> dict[str, Any]:
        """Explicit, cancellable long poll; backups exist only during this call.

        The first call seeds its baseline without reporting history. Reuse the
        returned cursor to drain subsequent batches without losing arrivals.
        The deadline includes backup time. A timed-out initial backup raises
        TimeoutError because no usable baseline has been established.
        """
        if type(timeout) not in (int, float) or not 0 < timeout <= 90:
            raise ValueError("SMS notification timeout must be greater than 0 and at most 90 seconds")
        if type(interval) is not int or not 10 <= interval <= 300:
            raise ValueError("SMS refresh interval must be 10..300 seconds")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")
        position = self._read_notification_cursor(cursor) if cursor is not None else None
        if self._stop.is_set():
            raise RuntimeError("MessageStore is closed")
        deadline = asyncio.timeout(timeout)
        refresh_before_wait = position is not None
        try:
            async with deadline:
                if position is None:
                    await self.refresh()
                    async with self._refresh_lock:
                        position = await asyncio.to_thread(self._notification_baseline_sync)
                else:
                    # Drain a previously refreshed batch before starting another
                    # USB backup. Concurrent readers/refreshes share this lock.
                    async with self._refresh_lock:
                        messages = await asyncio.to_thread(self._notification_batch_sync, *position, limit)
                    if messages:
                        last = messages[-1]
                        return {"messages": [self._protocol_message(m) for m in messages],
                                "cursor": self._notification_cursor(last["id"], last["guid"]), "timedOut": False}
                while not self._stop.is_set():
                    if not refresh_before_wait:
                        try:
                            await asyncio.wait_for(self._stop.wait(), timeout=interval)
                            break
                        except TimeoutError:
                            pass
                    refresh_before_wait = False
                    await self.refresh()
                    async with self._refresh_lock:
                        messages = await asyncio.to_thread(self._notification_batch_sync, *position, limit)
                    if messages:
                        last = messages[-1]
                        return {"messages": [self._protocol_message(m) for m in messages],
                                "cursor": self._notification_cursor(last["id"], last["guid"]), "timedOut": False}
                raise RuntimeError("MessageStore is closed")
        except TimeoutError:
            if position is None or not deadline.expired():
                raise
            return {"messages": [], "cursor": self._notification_cursor(*position), "timedOut": True}

    async def watch(self, interval: int = 60):
        """Yield newly backed-up messages; the first refresh seeds without events.

        A caller must iterate this async generator to start backups. It stops
        after ``close``. Polling frequency follows the analyzed 10–300 second
        range and defaults to 60 seconds.
        """
        if not isinstance(interval, int) or not 10 <= interval <= 300:
            raise ValueError("SMS refresh interval must be 10..300 seconds")
        if self._stop.is_set():
            raise RuntimeError("MessageStore is closed")
        await self.refresh()
        last_id = await asyncio.to_thread(self._max_id_sync)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            await self.refresh()
            messages = await asyncio.to_thread(self._new_sync, last_id)
            for message in messages:
                last_id = max(last_id, message["id"])
                yield message

    def start_polling(self, callback, interval: int = 60) -> None:
        """Start an explicitly requested watcher and deliver each new message."""
        if not isinstance(interval, int) or not 10 <= interval <= 300:
            raise ValueError("SMS refresh interval must be 10..300 seconds")
        if self._stop.is_set():
            raise RuntimeError("MessageStore is closed")
        if self._poll_task is not None and not self._poll_task.done():
            raise RuntimeError("SMS polling is already running")

        async def run() -> None:
            async for message in self.watch(interval):
                result = callback(message)
                if inspect.isawaitable(result):
                    await result

        self._poll_task = asyncio.create_task(run())

    async def close(self) -> None:
        self._stop.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
            self._poll_task = None

    async def dispatch(self, action: str, params: dict[str, Any]) -> Any:
        """Dispatch the optional read-only SMS controller actions."""
        p = dict(params)
        if action == "sms_refresh":
            return await self.refresh()
        if action == "sms_watch":
            return await self.wait_for_messages(cursor=p.get("cursor"), timeout=p.get("timeout", 90),
                                                interval=p.get("interval", 60), limit=p.get("limit", 200))
        if action == "sms_get":
            message = await self.get(p.get("id", p.get("messageId")))
            return None if message is None else self._protocol_message(message)
        if action == "sms_subscriptions":
            return await self.subscriptions()
        if action == "sms_list":
            result = await self.list(
                direction=p.get("direction") or "all",
                limit=p.get("limit") or 50,
                before=p.get("until", p.get("before")),
                after=p.get("since", p.get("after")),
                address=p.get("address"),
                thread=p.get("threadId", p.get("thread")),
                services=p.get("services"), stored_from=p.get("storedFrom"),
                kinds=p.get("kinds"), junk=p.get("junk", "include"),
                order=p.get("order") or "desc", cursor=p.get("cursor") or None,
            )
            response = {"messages": [self._protocol_message(message) for message in result["messages"]]}
            if result["next_cursor"] is not None:
                response["nextCursor"] = result["next_cursor"]
            return response
        raise SMSUnsupported(f"SMS action {action} is unavailable")
