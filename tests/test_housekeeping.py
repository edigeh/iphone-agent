"""The state directory stays bounded, and files a killed session left behind do not block the next one."""

import json
import os
import time

import pytest

from iphone_agent import apple_runner, audit, housekeeping, mcp_session, network


def test_journal_with_a_torn_last_record_is_set_aside_and_a_new_chain_starts(tmp_path):
    path = tmp_path / "audit.jsonl"
    first = audit.AuditLog(path)
    first.append("request", action="tap")
    first.close()
    kept = path.read_text()
    with path.open("a") as stream:
        stream.write('{"event": "resu')

    reopened = audit.AuditLog(path)
    assert reopened.set_aside is not None and reopened.set_aside.name.startswith("audit-damaged-")
    assert reopened.set_aside.read_text() == kept + '{"event": "resu'
    reopened.append("request", action="swipe")
    reopened.close()
    records = audit.AuditLog.verify(path)
    assert [record["action"] for record in records] == ["swipe"] and records[0]["previous"] == "0" * 64


def test_journal_with_a_broken_chain_is_set_aside_too(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = audit.AuditLog(path)
    log.append("request", action="tap")
    log.append("result", status="ok")
    log.close()
    lines = path.read_text().splitlines()
    path.write_text(lines[1] + "\n" + lines[0] + "\n")
    with pytest.raises(ValueError, match="Audit chain is damaged"):
        audit.AuditLog.verify(path)
    reopened = audit.AuditLog(path)
    assert reopened.set_aside.name.startswith("audit-damaged-") and reopened.last == "0" * 64
    reopened.close()


def test_journal_past_the_size_limit_rotates_with_its_chain_intact(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    log = audit.AuditLog(path)
    for index in range(20):
        log.append("request", index=index)
    log.close()
    monkeypatch.setattr(audit, "ROTATE_BYTES", path.stat().st_size - 1)

    rotated = audit.AuditLog(path)
    assert rotated.set_aside.name.startswith("audit-full-")
    assert len(audit.AuditLog.verify(rotated.set_aside)) == 20
    assert rotated.last == "0" * 64 and path.stat().st_size == 0
    rotated.close()


def test_healthy_journal_under_the_limit_continues_its_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = audit.AuditLog(path)
    last = log.append("request", action="tap")["hash"]
    log.close()
    reopened = audit.AuditLog(path)
    assert reopened.set_aside is None and reopened.last == last
    reopened.close()
    assert [entry.name for entry in tmp_path.iterdir()] == ["audit.jsonl"]


def test_journal_held_by_another_controller_is_reported_busy_and_left_alone(tmp_path):
    path = tmp_path / "audit.jsonl"
    holder = audit.AuditLog(path)
    holder.append("request", action="tap")
    with pytest.raises(audit.AuditLogBusy):
        audit.AuditLog(path)
    holder.close()
    assert len(audit.AuditLog.verify(path)) == 1


def test_launch_keeps_the_newest_logs_and_removes_only_day_old_launch_plans(tmp_path):
    now = time.time()
    for index in range(30):
        log = tmp_path / f"wda-xcodebuild-{index:02d}.log"
        log.write_text("log")
        os.utime(log, (now - index, now - index))
    fresh, stale = tmp_path / "wda-launch-fresh.xctestrun", tmp_path / "wda-launch-stale.xctestrun"
    fresh.write_text("plan")
    stale.write_text("plan")
    os.utime(stale, (now - 2 * 24 * 3600, now - 2 * 24 * 3600))
    other = tmp_path / "audit.jsonl"
    other.write_text("")

    apple_runner._prune_launch_files(tmp_path)

    logs = sorted(path.name for path in tmp_path.glob("wda-xcodebuild-*.log"))
    # One below the limit, so the log this launch is about to write makes twenty.
    assert logs == [f"wda-xcodebuild-{index:02d}.log" for index in range(19)]
    assert fresh.exists() and not stale.exists() and other.exists()


def test_released_session_lock_no_longer_names_a_live_holder(tmp_path):
    lock = mcp_session.SessionLock(tmp_path)
    assert lock._try_acquire()
    assert json.loads(lock.path.read_text()) == {"pid": os.getpid(), "state": "active"}
    lock.release()
    assert lock.path.read_text() == ""
    assert mcp_session.SessionLock(tmp_path)._holder() == {}


async def test_failed_lock_state_query_is_retried_and_a_failed_inventory_read_stays_a_binding_error(monkeypatch):
    class Process:
        async def wait(self):
            return 1

    async def spawn(*command, **options):
        return Process()

    monkeypatch.setattr(network.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(network.CoreDeviceUnavailableError, match="lockState query failed with exit status 1") as caught:
        await network._coredevice_query_once("PHONE", 5.0, "lockState")
    assert network.is_recoverable_error(caught.value)
    with pytest.raises(network.NetworkBindingError, match="devices query failed with exit status 1") as caught:
        await network._coredevice_query_once("PHONE", 5.0, "devices")
    assert not network.is_recoverable_error(caught.value)


def test_journal_rotated_by_another_controller_between_open_and_lock_is_neither_read_nor_renamed(tmp_path, monkeypatch):
    import fcntl
    path = tmp_path / "audit.jsonl"
    old = audit.AuditLog(path)
    old.append("request", action="tap")
    old.close()
    real_flock, moved = fcntl.flock, []

    def flock(fd, operation):
        if not moved:
            # The other controller rotates now: our descriptor points at the file it set aside.
            moved.append(tmp_path / "audit-full-other.jsonl")
            os.replace(path, moved[0])
            path.write_text("")
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", flock)
    log = audit.AuditLog(path)
    assert log.set_aside is None and os.fstat(log.fd).st_ino == path.stat().st_ino
    log.append("request", action="swipe")
    log.close()
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["audit-full-other.jsonl", "audit.jsonl"]
    assert [record["action"] for record in audit.AuditLog.verify(path)] == ["swipe"]
    assert [record["action"] for record in audit.AuditLog.verify(moved[0])] == ["tap"]


DAY = 24 * 3600.0


def _aged(path, days):
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))
    return path


def _build(home, name, days, size=0):
    directory = home / "build" / name
    product = directory / "Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app"
    product.mkdir(parents=True)
    (product / "binary").write_bytes(b"b" * size)
    _aged(directory, days)
    return directory, product


def test_builds_keep_the_saved_runner_and_any_setup_in_progress(tmp_path):
    used, product = _build(tmp_path, "used", 20, size=10)
    stale, _ = _build(tmp_path, "stale", 3, size=700)
    fresh, _ = _build(tmp_path, "fresh", 0.1)
    (tmp_path / "build" / ".DS_Store").write_bytes(b"finder")
    (tmp_path / "runner.json").write_text(json.dumps({"appPath": str(product)}))

    removed = housekeeping.prune_builds(tmp_path)

    assert removed == [(stale, 700)]
    assert used.exists() and fresh.exists() and not stale.exists()
    assert (tmp_path / "build" / ".DS_Store").exists()


def test_builds_are_left_alone_when_the_saved_runner_cannot_be_read(tmp_path):
    stale, _ = _build(tmp_path, "stale", 3)
    (tmp_path / "runner.json").write_text('{"appPath": ')
    assert housekeeping.prune_builds(tmp_path) == []
    assert stale.exists()


def test_builds_extra_kept_paths_survive(tmp_path):
    previous, previous_product = _build(tmp_path, "previous", 5)
    current, product = _build(tmp_path, "current", 0)
    (tmp_path / "runner.json").write_text(json.dumps({"appPath": str(product)}))
    assert housekeeping.prune_builds(tmp_path, keep=[previous_product]) == []
    assert previous.exists()


def test_build_directory_link_is_never_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("owner file")
    (tmp_path / "build").mkdir()
    link = tmp_path / "build" / "linked"
    link.symlink_to(outside)
    os.utime(link, (time.time() - 5 * DAY,) * 2, follow_symlinks=False)
    housekeeping.prune_builds(tmp_path)
    assert (outside / "keep").exists()


def test_recordings_older_than_the_limit_are_removed_and_others_kept(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    old = recordings / "recording-old.mp4"
    old.write_bytes(b"o" * 50)
    _aged(old, 31)
    recent = recordings / "recording-recent.mp4"
    recent.write_bytes(b"r")
    _aged(recent, 29)
    owner = recordings / "notes.txt"
    owner.write_text("not a recording")
    _aged(owner, 400)

    assert housekeeping.prune_recordings(recordings, 30) == [(old, 50)]
    assert recent.exists() and owner.exists() and not old.exists()


def test_recording_limit_zero_keeps_everything(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    old = recordings / "recording-old.mp4"
    old.write_bytes(b"o")
    _aged(old, 999)
    assert housekeeping.prune_recordings(recordings, 0) == []
    assert housekeeping.prune_recordings(tmp_path / "missing", 30) == []
    assert old.exists()


def test_tidy_removes_old_builds_and_recordings_and_writes_one_entry(tmp_path, caplog):
    stale, _ = _build(tmp_path, "stale", 3, size=5)
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    old = recordings / "recording-a.mp4"
    old.write_bytes(b"x" * 7)
    _aged(old, 40)
    entries = []

    class Journal:
        def append(self, event, **fields):
            entries.append({"event": event, **fields})

    housekeeping.tidy_state_directory(tmp_path, 30, Journal())

    assert not stale.exists() and not old.exists()
    assert entries == [{"event": "housekeeping", "keepRecordingsDays": 30,
                        "builds": {"count": 1, "bytes": 5}, "recordings": {"count": 1, "bytes": 7}}]
    removals = [record.getMessage() for record in caplog.records if "Removed" in record.getMessage()]
    assert len(removals) == 2

    entries.clear()
    housekeeping.tidy_state_directory(tmp_path, 30, Journal())
    assert entries == []


def test_tidy_failure_is_logged_and_never_raised(tmp_path, monkeypatch, caplog):
    def broken(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(housekeeping, "prune_builds", broken)
    housekeeping.tidy_state_directory(tmp_path, 30, None)
    assert any("denied" in record.getMessage() for record in caplog.records)


def test_saved_build_is_kept_when_the_state_directory_is_reached_through_a_link(tmp_path):
    real = tmp_path / "real-home"
    real.mkdir()
    linked = tmp_path / "linked-home"
    linked.symlink_to(real)
    used, product = _build(real, "used", 5)
    # Setup saves the resolved path; the controller is started with the path as typed.
    (real / "runner.json").write_text(json.dumps({"appPath": str(product.resolve())}))
    assert housekeeping.prune_builds(linked) == []
    (real / "runner.json").write_text(json.dumps({"appPath": str(linked / product.relative_to(real))}))
    assert housekeeping.prune_builds(real) == []
    assert used.exists()


def test_build_with_an_unreadable_directory_is_still_removed(tmp_path):
    stale, product = _build(tmp_path, "stale", 3, size=9)
    sealed = stale / "sealed"
    (sealed / "inner").mkdir(parents=True)
    (sealed / "inner" / "file").write_bytes(b"z")
    sealed.chmod(0)
    _aged(stale, 3)
    try:
        removed = housekeeping.prune_builds(tmp_path)
    finally:
        if sealed.exists():
            sealed.chmod(0o700)
    assert [path for path, _ in removed] == [stale] and not stale.exists()
