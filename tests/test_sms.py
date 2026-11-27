import sqlite3
import stat
from pathlib import Path

import pytest

from iphone_agent.sms import MessageStore, SMSUnsupported


@pytest.fixture
def store(tmp_path: Path) -> MessageStore:
    backup = tmp_path / "backup"
    device = backup / "test-udid"
    (device / "ab").mkdir(parents=True)
    manifest = sqlite3.connect(device / "Manifest.db")
    manifest.execute("CREATE TABLE Files (fileID TEXT, domain TEXT, relativePath TEXT)")
    file_id = "ab" + "0" * 38
    manifest.execute("INSERT INTO Files VALUES (?, ?, ?)",
                     (file_id, "HomeDomain", "Library/SMS/sms.db"))
    manifest.commit()
    manifest.close()
    sms = sqlite3.connect(device / "ab" / file_id)
    sms.executescript("""
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, subject TEXT,
                              date INTEGER, is_from_me INTEGER, handle_id INTEGER, service TEXT);
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        INSERT INTO handle VALUES (1, '+15550001'), (2, '+15550002');
        INSERT INTO chat VALUES (10, 'thread-1'), (11, 'thread-2');
        INSERT INTO message VALUES (1, 'guid-1', 'hello', NULL, 1000000000, 0, 1, 'SMS');
        INSERT INTO message VALUES (2, 'guid-2', 'reply', NULL, 1000000000000000000, 1, 1, 'iMessage');
        INSERT INTO message VALUES (3, 'guid-3', 'other', NULL, 1000000001000000000, 0, 2, 'SMS');
        INSERT INTO chat_message_join VALUES (10, 1), (10, 2), (11, 3);
    """)
    sms.commit()
    sms.close()
    return MessageStore("test-udid", backup)


@pytest.mark.asyncio
async def test_read_only_sms_list_filters_and_cursor(store):
    first = await store.list(limit=2)
    assert [message["id"] for message in first["messages"]] == [3, 2]
    assert isinstance(first["next_cursor"], str)
    second = await store.list(limit=2, cursor=first["next_cursor"])
    assert [message["id"] for message in second["messages"]] == [1]
    assert second["next_cursor"] is None
    assert [message["id"] for message in (await store.list(direction="outgoing"))["messages"]] == [2]
    assert [message["id"] for message in (await store.list(address="+15550001"))["messages"]] == [2, 1]
    assert [message["id"] for message in (await store.list(thread="thread-2"))["messages"]] == [3]
    assert [message["id"] for message in (await store.list(before=1978307200500))["messages"]] == [2, 1]
    assert [message["id"] for message in (await store.list(after=1978307200500))["messages"]] == [3]
    assert (await store.get(2))["text"] == "reply"
    assert (await store.get(999)) is None


@pytest.mark.asyncio
async def test_snapshot_rejects_invalid_manifest_and_unsupported_subscriptions(store):
    with pytest.raises(ValueError, match="direction"):
        await store.list(direction="sideways")
    with pytest.raises(ValueError, match="limit"):
        await store.list(limit=1000)
    store.backup_dir.chmod(0o700)
    store.device_dir.chmod(0o700)
    with pytest.raises(SMSUnsupported, match="SIM subscriptions"):
        await store.subscriptions()
    manifest = sqlite3.connect(store.device_dir / "Manifest.db")
    manifest.execute("UPDATE Files SET fileID = '../escape'")
    manifest.commit()
    manifest.close()
    with pytest.raises(ValueError, match="invalid SMS fileID"):
        await store.list()


@pytest.mark.asyncio
async def test_sms_dispatch_accepts_protocol_id_and_list_fields(store):
    message = await store.dispatch("sms_get", {"id": "guid-2"})
    assert message["id"] == "guid-2" and message["body"] == "reply"
    first_incoming = await store.dispatch("sms_list", {"direction": "incoming", "limit": 1})
    result = await store.dispatch("sms_list", {"direction": "incoming", "limit": 1,
                                               "cursor": first_incoming["nextCursor"]})
    assert [item["id"] for item in result["messages"]] == ["guid-1"]
    assert [item["id"] for item in (await store.dispatch(
        "sms_list", {"threadId": "thread-1"}))["messages"]] == ["guid-2", "guid-1"]
    filtered = await store.dispatch("sms_list", {"services": ["SMS"]})
    assert [item["id"] for item in filtered["messages"]] == ["guid-3", "guid-1"]
    first_sms = await store.dispatch("sms_list", {"services": ["SMS"], "limit": 1})
    second_sms = await store.dispatch("sms_list", {"services": ["SMS"], "limit": 1,
                                                   "cursor": first_sms["nextCursor"]})
    assert [item["id"] for item in second_sms["messages"]] == ["guid-1"]
    assert (await store.dispatch("sms_list", {"services": ["sms"]}))["messages"] == []
    with pytest.raises(ValueError, match="kinds"):
        await store.dispatch("sms_list", {"kinds": ["text"]})
    store.backup_dir.chmod(0o700)
    store.device_dir.chmod(0o700)
    with pytest.raises(SMSUnsupported, match="SIM subscriptions"):
        await store.dispatch("sms_subscriptions", {})
    await store.close()


@pytest.mark.asyncio
async def test_sms_date_order_tie_break_and_cursor_ceiling(store):
    with sqlite3.connect(store._sms_db_path()) as database:
        database.execute(
            "INSERT INTO message VALUES (4, 'guid-4', 'older', NULL, 900000000, 0, 1, 'SMS')"
        )
    ascending = await store.list(order="asc", limit=2)
    assert [message["id"] for message in ascending["messages"]] == [4, 1]
    token = ascending["next_cursor"]
    filters = store._filter_digest(direction="all", before=None, after=None,
                                   address=None, thread=None, services=[], stored_from=None)
    assert store._decode_cursor(token, "asc", filters)["c"] == 4
    descending = await store.list(order="desc", limit=2)
    assert [message["id"] for message in descending["messages"]] == [3, 2]
    assert [message["id"] for message in
            (await store.list(order="desc", cursor=descending["next_cursor"]))["messages"]] == [1, 4]
    assert ascending["messages"][1]["timestamp_ms"] == descending["messages"][1]["timestamp_ms"]
    with sqlite3.connect(store._sms_db_path()) as database:
        database.execute(
            "INSERT INTO message VALUES (5, 'guid-5', 'older arrival', NULL, 800000000, 0, 1, 'SMS')"
        )
    next_page = await store.list(order="ascending", limit=2, cursor=token)
    assert [message["id"] for message in next_page["messages"]] == [2, 3]
    with pytest.raises(ValueError, match="cursor"):
        await store.list(order="desc", cursor=token)
    with pytest.raises(ValueError, match="cursor"):
        await store.list(order="asc", services=["SMS"], cursor=token)
    with pytest.raises(ValueError, match="cursor"):
        await store.list(cursor="3")


@pytest.mark.asyncio
async def test_sms_first_seen_index_is_persisted_and_only_added_by_refresh(store, monkeypatch):
    import iphone_agent.sms as sms_module

    with pytest.raises(SMSUnsupported, match="indexed SMS refresh"):
        await store.list(stored_from=1)
    monkeypatch.setattr(sms_module.time, "time_ns", lambda: 1_700_000_000_000_000_000)

    class FakeDevice:
        serial = "test-udid"
        connection_type = "USB"

        def matches_udid(self, udid):
            return udid == self.serial

    class FakeLockdown:
        paired = True

        async def close(self):
            pass

    class FakeBackup:
        def __init__(self, lockdown):
            assert isinstance(lockdown, FakeLockdown)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get_will_encrypt(self):
            return False

        @staticmethod
        def selection_filter_callback(_rules):
            return lambda *_: True

        async def backup(self, **_kwargs):
            pass  # The fixture already contains the synthetic SMS database.

    async def fake_devices():
        return [FakeDevice()]

    async def fake_connect(**_kwargs):
        return FakeLockdown()

    store.backup_dir.chmod(0o700)
    store.device_dir.chmod(0o700)
    monkeypatch.setattr(sms_module, "list_devices", fake_devices)
    monkeypatch.setattr(sms_module, "create_using_usbmux", fake_connect)
    monkeypatch.setattr(sms_module, "Mobilebackup2Service", FakeBackup)
    await store.refresh()
    assert stat.S_IMODE(store._seen_path.stat().st_mode) == 0o600
    stale_cursor = (await store.list(limit=1))["next_cursor"]
    old = await store.dispatch("sms_get", {"id": "guid-1"})
    assert old["storedAt"] == 1_700_000_000_000
    with sqlite3.connect(store._sms_db_path()) as database:
        database.execute(
            "INSERT INTO message VALUES (4, 'guid-4', 'new', NULL, 1000000002000000000, 0, 1, 'SMS')"
        )
        database.execute("UPDATE message SET date = 900000000 WHERE ROWID = 3")
    monkeypatch.setattr(sms_module.time, "time_ns", lambda: 1_700_000_060_000_000_000)
    await store.refresh()
    with pytest.raises(ValueError, match="stale"):
        await store.list(cursor=stale_cursor)
    assert (await store.dispatch("sms_get", {"id": "guid-1"}))["storedAt"] == 1_700_000_000_000
    recent = await store.dispatch("sms_list", {"storedFrom": 1_700_000_030_000})
    assert [item["id"] for item in recent["messages"]] == ["guid-4"]
    assert recent["messages"][0]["storedAt"] == 1_700_000_060_000


@pytest.mark.asyncio
async def test_watch_seeds_history_then_emits_new_message_only(store, monkeypatch):
    import iphone_agent.sms as sms_module

    refreshes = 0

    async def fake_refresh():
        nonlocal refreshes
        refreshes += 1
        if refreshes == 2:
            sms_path = store._sms_db_path()
            with sqlite3.connect(sms_path) as database:
                database.execute("INSERT INTO message VALUES (4, 'guid-4', 'new', NULL, 1000000002000000000, 0, 1, 'SMS')")

    async def fast_wait(_awaitable, timeout):
        assert timeout == 10
        _awaitable.close()
        raise sms_module.asyncio.TimeoutError

    monkeypatch.setattr(store, "refresh", fake_refresh)
    monkeypatch.setattr(sms_module.asyncio, "wait_for", fast_wait)
    watcher = store.watch(10)
    try:
        newest = await watcher.__anext__()
        assert newest["guid"] == "guid-4"
        assert refreshes == 2
    finally:
        await watcher.aclose()


@pytest.mark.asyncio
async def test_refresh_filters_backup_and_requires_selected_paired_usb(monkeypatch, tmp_path):
    import iphone_agent.sms as sms_module

    class FakeDevice:
        serial = "test-udid"
        connection_type = "USB"

        def matches_udid(self, udid):
            return udid == self.serial

    class FakeLockdown:
        paired = True
        closed = False

        async def close(self):
            self.closed = True

    client = FakeLockdown()
    calls = []

    async def fake_devices():
        return [FakeDevice()]

    async def fake_connect(**kwargs):
        calls.append(("connect", kwargs))
        return client

    class FakeBackup:
        def __init__(self, lockdown):
            assert lockdown is client

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get_will_encrypt(self):
            return True

    monkeypatch.setattr(sms_module, "list_devices", fake_devices)
    monkeypatch.setattr(sms_module, "create_using_usbmux", fake_connect)
    monkeypatch.setattr(sms_module, "Mobilebackup2Service", FakeBackup)
    store = MessageStore("test-udid", tmp_path / "private")
    with pytest.raises(SMSUnsupported, match="encrypted"):
        await store.refresh()
    assert not store._seen_path.exists()
    assert calls[0][1]["serial"] == "test-udid"
    assert calls[0][1]["connection_type"] == "USB"
    assert calls[0][1]["autopair"] is False
    assert client.closed


@pytest.mark.asyncio
async def test_notification_long_poll_seeds_then_delivers_batches(store, monkeypatch):
    import asyncio
    import iphone_agent.sms as sms_module

    refreshes = 0

    async def fake_refresh():
        nonlocal refreshes
        refreshes += 1
        if refreshes == 2:
            with sqlite3.connect(store._sms_db_path()) as database:
                database.executemany(
                    "INSERT INTO message VALUES (?, ?, ?, NULL, 1000000002000000000, 0, 1, 'SMS')",
                    [(4, "guid-4", "new one"), (5, "guid-5", "new two")],
                )

    async def immediate_interval(awaitable, timeout):
        assert timeout == 10
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(store, "refresh", fake_refresh)
    monkeypatch.setattr(sms_module.asyncio, "wait_for", immediate_interval)
    result = await store.dispatch("sms_watch", {"timeout": 5, "interval": 10, "limit": 1})
    assert [m["id"] for m in result["messages"]] == ["guid-4"]
    assert refreshes == 2
    second = await store.dispatch("sms_watch", {"cursor": result["cursor"], "limit": 1})
    assert [m["id"] for m in second["messages"]] == ["guid-5"]
    assert second["timedOut"] is False
    assert refreshes == 2  # Remaining batch is drained without another backup.
    await store.close()


@pytest.mark.asyncio
async def test_notification_timeout_cursor_resume_refresh_and_validation(store, monkeypatch):
    refreshes = 0

    async def fake_refresh():
        nonlocal refreshes
        refreshes += 1
        if refreshes == 2:
            with sqlite3.connect(store._sms_db_path()) as database:
                database.execute("INSERT INTO message VALUES (4, 'guid-4', 'new', NULL, 1000000002000000000, 0, 1, 'SMS')")

    monkeypatch.setattr(store, "refresh", fake_refresh)
    first = await store.wait_for_messages(timeout=1, interval=10)
    assert first["messages"] == [] and first["timedOut"]
    second = await store.wait_for_messages(cursor=first["cursor"], timeout=1, interval=10)
    assert [m["id"] for m in second["messages"]] == ["guid-4"]
    assert refreshes == 2  # Resumed polls refresh immediately, before waiting.
    other = MessageStore("another-udid", store.backup_dir)
    with pytest.raises(ValueError, match="phone"):
        await other.wait_for_messages(cursor=first["cursor"])
    with sqlite3.connect(store._sms_db_path()) as database:
        database.execute("UPDATE message SET guid='changed' WHERE ROWID=4")
    with pytest.raises(ValueError, match="stale"):
        await store.wait_for_messages(cursor=second["cursor"])
    for params in ({"timeout": 0}, {"timeout": 91}, {"timeout": float('nan')},
                   {"interval": 9}, {"interval": True}, {"limit": 201}, {"cursor": "bad"}):
        with pytest.raises(ValueError):
            await store.wait_for_messages(**params)
    await store.close()


@pytest.mark.asyncio
async def test_notification_cancellation_stops_backup_and_does_not_mask_backup_timeout(store, monkeypatch):
    import asyncio

    started, cleaned = asyncio.Event(), asyncio.Event()

    async def blocked_refresh():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(store, "refresh", blocked_refresh)
    task = asyncio.create_task(store.wait_for_messages(timeout=1))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set() and store._poll_task is None
    with pytest.raises(TimeoutError):
        await store.wait_for_messages(timeout=0.01)

    async def broken_refresh():
        raise TimeoutError("backup transport timed out")

    monkeypatch.setattr(store, "refresh", broken_refresh)
    token = store._notification_cursor(3, 'guid-3')
    with pytest.raises(TimeoutError, match="transport"):
        await store.wait_for_messages(cursor=token, timeout=1)
    await store.close()
    with pytest.raises(RuntimeError, match="closed"):
        await store.wait_for_messages()


@pytest.fixture
def backup_service(store, monkeypatch):
    import iphone_agent.sms as sms_module

    class FakeDevice:
        serial = "test-udid"
        connection_type = "USB"

        def matches_udid(self, udid):
            return udid == self.serial

    class FakeLockdown:
        paired = True

        async def close(self):
            pass

    class FakeBackup:
        effect = None

        def __init__(self, _lockdown):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get_will_encrypt(self):
            return False

        @staticmethod
        def selection_filter_callback(_rules):
            return lambda *_: True

        async def backup(self, **_kwargs):
            assert store._dirty_path.is_file()
            assert stat.S_IMODE(store._dirty_path.stat().st_mode) == 0o600
            if type(self).effect is not None:
                await type(self).effect()

    async def fake_devices():
        return [FakeDevice()]

    async def fake_connect(**_kwargs):
        return FakeLockdown()

    store.backup_dir.chmod(0o700)
    store.device_dir.chmod(0o700)
    monkeypatch.setattr(sms_module, "list_devices", fake_devices)
    monkeypatch.setattr(sms_module, "create_using_usbmux", fake_connect)
    monkeypatch.setattr(sms_module, "Mobilebackup2Service", FakeBackup)
    return FakeBackup


@pytest.mark.asyncio
async def test_failed_refresh_blocks_snapshot_and_old_cursor_until_success(store, backup_service):
    await store.refresh()
    cursor = (await store.list(limit=1))["next_cursor"]

    async def partial_backup():
        with sqlite3.connect(store._sms_db_path(allow_dirty=True)) as database:
            database.execute("INSERT INTO message VALUES (4, 'guid-4', 'partial', NULL, 1000000002000000000, 0, 1, 'SMS')")
        raise OSError("backup failed after writing")

    backup_service.effect = partial_backup
    with pytest.raises(OSError, match="after writing"):
        await store.refresh()
    assert store._dirty_path.exists()
    reopened = MessageStore(store.udid, store.backup_dir)
    for read in (store.list(cursor=cursor), store.get('guid-1'), reopened.list(),
                 store.wait_for_messages(cursor=store._notification_cursor(3, 'guid-3'))):
        with pytest.raises(SMSUnsupported, match="incomplete"):
            await read
    backup_service.effect = None
    assert await store.refresh() == {"messages": 4}
    assert not store._dirty_path.exists()
    assert (await store.get('guid-4'))["text"] == "partial"
    with pytest.raises(ValueError, match="stale"):
        await store.list(cursor=cursor)


@pytest.mark.asyncio
async def test_cancelled_backup_retains_dirty_marker_and_success_recovers(store, backup_service):
    import asyncio

    started = asyncio.Event()

    async def blocked_backup():
        started.set()
        await asyncio.Event().wait()

    backup_service.effect = blocked_backup
    task = asyncio.create_task(store.refresh())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store._dirty_path.exists()
    with pytest.raises(SMSUnsupported, match="incomplete"):
        await store.get('guid-1')
    backup_service.effect = None
    await store.refresh()
    assert not store._dirty_path.exists()
    assert (await store.list())["messages"]


@pytest.mark.asyncio
async def test_dirty_marker_rejects_symlink_and_public_permissions(store, backup_service, tmp_path):
    target = tmp_path / 'unrelated'
    target.write_text('preserve')
    store._dirty_path.symlink_to(target)
    with pytest.raises(PermissionError, match="marker"):
        await store.refresh()
    with pytest.raises(PermissionError, match="marker"):
        await store.list()
    assert target.read_text() == 'preserve'
    store._dirty_path.unlink()
    store._dirty_path.write_text('incomplete')
    store._dirty_path.chmod(0o644)
    with pytest.raises(PermissionError, match="marker"):
        await store.refresh()
    with pytest.raises(PermissionError, match="marker"):
        await store.get('guid-1')
    store._dirty_path.chmod(0o600)
    await store.refresh()
    assert not store._dirty_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('step', ['_record_seen_sync', '_count_sync'])
async def test_index_or_count_failure_keeps_snapshot_dirty(store, backup_service, monkeypatch, step):
    original = getattr(store, step)

    def broken(*, allow_dirty=False):
        assert allow_dirty
        raise OSError('post-backup validation failed')

    monkeypatch.setattr(store, step, broken)
    with pytest.raises(OSError, match='validation failed'):
        await store.refresh()
    assert store._dirty_path.exists()
    with pytest.raises(SMSUnsupported, match='incomplete'):
        await store.list()
    monkeypatch.setattr(store, step, original)
    await store.refresh()
    assert not store._dirty_path.exists()


@pytest.mark.asyncio
async def test_subscriptions_use_only_completed_private_matching_backup_metadata(store):
    import plistlib

    store.backup_dir.chmod(0o700)
    store.device_dir.chmod(0o700)
    path = store.device_dir / 'Info.plist'
    with pytest.raises(SMSUnsupported, match='metadata'):
        await MessageStore('missing-phone', store.backup_dir).subscriptions()
    for number, expected in [('+1 (555) 000-0001', '+15550000001'),
                             ('0055 11 99999-0000', '+5511999990000')]:
        path.write_bytes(plistlib.dumps({'Target Identifier': 'TEST-UDID', 'Phone Number': number}))
        assert await store.dispatch('sms_subscriptions', {}) == {'subscriptions': [{'phoneNumber': expected}]}
    path.write_bytes(plistlib.dumps({'Target Identifier': 'different-phone', 'Phone Number': '+15550000001'}))
    with pytest.raises(ValueError, match='identifier'):
        await store.subscriptions()
    path.write_bytes(plistlib.dumps({'Target Identifier': store.udid, 'Phone Number': '5550000001'}))
    with pytest.raises(SMSUnsupported, match='unambiguous'):
        await store.subscriptions()
    path.write_bytes(plistlib.dumps({'Target Identifier': store.udid}))
    with pytest.raises(SMSUnsupported, match='metadata'):
        await store.subscriptions()
    store._mark_refresh_dirty()
    with pytest.raises(SMSUnsupported, match='incomplete'):
        await store.subscriptions()
    store._clear_refresh_dirty()
    path.unlink()
    path.symlink_to(store.device_dir / 'Manifest.db')
    with pytest.raises(OSError):
        await store.subscriptions()


@pytest.fixture
def classified_store(store):
    with sqlite3.connect(store._sms_db_path()) as database:
        database.executescript('''
            ALTER TABLE message ADD COLUMN item_type INTEGER;
            ALTER TABLE message ADD COLUMN associated_message_type INTEGER;
            ALTER TABLE message ADD COLUMN is_spam INTEGER;
            ALTER TABLE message ADD COLUMN filter_action INTEGER;
            ALTER TABLE chat ADD COLUMN is_filtered INTEGER;
            ALTER TABLE chat ADD COLUMN is_blackholed INTEGER;
            UPDATE message SET is_spam=1 WHERE ROWID=2;
            UPDATE message SET filter_action=2 WHERE ROWID=3;
            INSERT INTO chat VALUES (12,'junk-chat',2,0),(13,'unknown-chat',1,0),(14,'blocked-chat',0,1);
        ''')
        for row_id, item_type, associated, spam, action in [
            (4,0,999,0,0),(5,0,1000,0,0),(6,1,4000,0,0),(7,1,4001,0,0),
            (8,1,0,0,0),(9,None,None,None,None),(10,0,0,0,0),(11,0,0,0,0),
            (12,0,0,0,0),(13,0,0,1,0),(14,0,0,0,1),(15,0,2000,0,0),
        ]:
            database.execute(
                'INSERT INTO message (ROWID,guid,text,date,is_from_me,handle_id,service,item_type,associated_message_type,is_spam,filter_action) '
                "VALUES (?, ?, 'body', 1000000003000000000, 0, 1, 'SMS', ?, ?, ?, ?)",
                (row_id, 'guid-'+str(row_id),item_type,associated,spam,action),
            )
        database.executemany('INSERT INTO chat_message_join VALUES (?,?)',
                             [(12,10),(13,11),(14,12),(13,13),(13,15)])
    return store


@pytest.mark.asyncio
async def test_message_kind_boundaries_and_default(classified_store):
    store=classified_store
    all_kinds=['message','reaction','system']
    result=await store.list(kinds=all_kinds,limit=200)
    by_id={message['id']:message for message in result['messages']}
    assert by_id[4]['kind']=='message'  # Below reaction range.
    assert by_id[5]['kind']=='reaction'  # Inclusive lower bound.
    assert by_id[6]['kind']=='reaction'  # Inclusive upper bound, ahead of item_type.
    assert by_id[7]['kind']=='system' and by_id[8]['kind']=='system'
    assert by_id[9]['kind']=='message'  # Verified absent/NULL fallback.
    assert {m['id'] for m in (await store.list())['messages']}=={1,2,3,4,9,10,11,12,13,14}
    assert {m['id'] for m in (await store.list(kinds=[]))['messages']}=={1,2,3,4,9,10,11,12,13,14}
    assert {m['id'] for m in (await store.list(kinds=['reaction']))['messages']}=={5,6,15}
    assert {m['id'] for m in (await store.list(kinds=['reaction','reaction']))['messages']}=={5,6,15}
    assert {m['id'] for m in (await store.list(kinds=['system']))['messages']}=={7,8}


@pytest.mark.asyncio
async def test_junk_unknown_sender_and_protocol_flags(classified_store):
    store=classified_store
    junk=await store.list(junk='only')
    assert {m['id'] for m in junk['messages']}=={2,3,10,12,13}
    clean=await store.list(junk='exclude')
    assert {m['id'] for m in clean['messages']}=={1,4,9,11,14}
    unknown=await store.list(junk='unknownSender')
    assert [m['id'] for m in unknown['messages']]==[11]
    reaction=await store.dispatch('sms_list',{'kinds':['reaction'],'junk':'unknownSender'})
    assert [m['id'] for m in reaction['messages']]==['guid-15']
    message=await store.dispatch('sms_get',{'id':'guid-13'})
    assert message['kind']=='message' and message['junk'] is True and message['unknownSender'] is False
    message=await store.dispatch('sms_get',{'id':'guid-11'})
    assert message['junk'] is False and message['unknownSender'] is True


@pytest.mark.asyncio
async def test_classification_filters_bind_cursor_and_validate_values(classified_store):
    store=classified_store
    first=await store.list(kinds=['reaction'],junk='include',limit=1)
    second=await store.list(kinds=['reaction'],junk='include',limit=1,cursor=first['next_cursor'])
    assert first['messages'][0]['id']!=second['messages'][0]['id']
    for params in ({'kinds':['system']},{'kinds':['reaction'],'junk':'exclude'}):
        with pytest.raises(ValueError,match='cursor'):
            await store.list(cursor=first['next_cursor'],**params)
    for params in ({'kinds':['bad']},{'kinds':['message',{}]},{'kinds':'message'},
                   {'junk':'all'},{'junk':{}},{'junk':True},{'junk':False}):
        with pytest.raises(ValueError):
            await store.list(**params)
        with pytest.raises(ValueError):
            await store.dispatch('sms_list',params)


@pytest.mark.asyncio
async def test_classification_optional_columns_and_incomplete_chat_schema(store):
    result=await store.dispatch('sms_list',{})
    assert all(m['kind']=='message' and m['junk'] is False and m['unknownSender'] is False
               for m in result['messages'])
    assert (await store.list(junk='only'))['messages']==[]
    assert (await store.list(junk='unknownSender'))['messages']==[]
    with sqlite3.connect(store._sms_db_path()) as database:
        database.executescript('ALTER TABLE chat ADD COLUMN is_blackholed INTEGER; UPDATE chat SET is_blackholed=1;')
    assert (await store.get(1))['junk'] is False
    with sqlite3.connect(store._sms_db_path()) as database:
        database.executescript('ALTER TABLE chat ADD COLUMN is_filtered INTEGER; UPDATE chat SET is_filtered=2;')
    assert (await store.get(1))['junk'] is True
    with sqlite3.connect(store._sms_db_path()) as database:
        database.executescript('ALTER TABLE chat_message_join RENAME COLUMN message_id TO unsupported;')
    assert (await store.get(1))['junk'] is False
