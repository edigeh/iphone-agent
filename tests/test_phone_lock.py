"""One controller per phone, whatever state directory each controller uses."""

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap

import pytest

from iphone_agent import locks
from iphone_agent.core import ConnectionUnavailable


UDID = "00008120-0123456789ABCDEF"


async def test_second_controller_on_the_same_phone_is_refused_at_once_naming_the_holder(tmp_path):
    first = locks.PhoneLock(UDID, mode="mcp", home=tmp_path / "a")
    await first.acquire(5)
    second = locks.PhoneLock(UDID, mode="serve", home=tmp_path / "b")
    started = asyncio.get_running_loop().time()
    with pytest.raises(locks.PhoneBusy) as caught:
        await second.acquire(5)
    assert asyncio.get_running_loop().time() - started < 1
    message = str(caught.value)
    assert isinstance(caught.value, ConnectionUnavailable)
    assert f"pid {os.getpid()}" in message and "mcp" in message and str(tmp_path / "a") in message
    assert "--idle-timeout" in message and "retry" in message
    first.release()
    await second.acquire(5)
    second.release()


async def test_a_serve_holder_is_named_with_how_to_stop_it(tmp_path):
    holder = locks.PhoneLock(UDID, mode="serve", home=tmp_path)
    await holder.acquire(5)
    with pytest.raises(locks.PhoneBusy, match=r"iphone-agent --home \S+ call shutdown"):
        await locks.PhoneLock(UDID, mode="mcp", home=tmp_path / "other").acquire(5)
    holder.release()


async def test_two_state_directories_meet_at_one_lock_and_other_phones_are_free(tmp_path, private_phone_locks):
    holder = locks.PhoneLock(UDID, mode="serve", home=tmp_path / "a")
    await holder.acquire(5)
    assert holder.path.parent == private_phone_locks
    other_phone = locks.PhoneLock("OTHER-PHONE", mode="serve", home=tmp_path / "a")
    await other_phone.acquire(5)
    other_phone.release()
    holder.release()


async def test_holder_that_is_cleaning_up_is_waited_for(tmp_path):
    holder = locks.PhoneLock(UDID, mode="mcp", home=tmp_path)
    await holder.acquire(5)
    holder.mark_cleaning()
    asyncio.get_running_loop().call_later(0.3, holder.release)
    waiter = locks.PhoneLock(UDID, mode="serve", home=tmp_path / "b")
    assert await waiter.acquire(5) is True
    assert json.loads(waiter.path.read_text())["mode"] == "serve"
    waiter.release()


async def test_cleanup_that_outlasts_the_wait_gives_a_retryable_error(tmp_path):
    holder = locks.PhoneLock(UDID, mode="mcp", home=tmp_path)
    await holder.acquire(5)
    holder.mark_cleaning()
    with pytest.raises(locks.PhoneBusy, match="still closing"):
        await locks.PhoneLock(UDID, mode="serve", home=tmp_path).acquire(0.3)
    holder.release()


async def test_lock_left_by_a_killed_controller_does_not_block(tmp_path, private_phone_locks):
    script = textwrap.dedent(f"""
        import asyncio, sys
        from iphone_agent import locks
        lock = locks.PhoneLock({UDID!r}, mode="serve", home={str(tmp_path)!r})
        asyncio.run(lock.acquire(5))
        print("held", flush=True)
        sys.stdin.read()
    """)
    process = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               env={**os.environ, "IPHONE_AGENT_LOCK_DIR": str(private_phone_locks)})
    try:
        assert process.stdout.readline() == b"held\n"
        with pytest.raises(locks.PhoneBusy, match=f"pid {process.pid}"):
            await locks.PhoneLock(UDID, mode="mcp", home=tmp_path).acquire(1)
    finally:
        process.send_signal(signal.SIGKILL)
        process.wait()
    survivor = locks.PhoneLock(UDID, mode="mcp", home=tmp_path)
    await survivor.acquire(1)
    survivor.release()


def test_default_location_is_shared_by_every_state_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("IPHONE_AGENT_LOCK_DIR")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert locks.PhoneLock(UDID, mode="serve", home=tmp_path / "x").path == \
        tmp_path / ".iphone-agent" / "phones" / f"{UDID}.lock"


def test_identifier_that_is_not_a_plain_name_cannot_leave_the_lock_directory(private_phone_locks):
    path = locks.PhoneLock("../../etc/passwd", mode="serve", home=Path("/tmp")).path
    assert path.parent == private_phone_locks and ".." not in path.name


async def test_lock_whose_holder_file_cannot_be_written_is_not_left_held(tmp_path, monkeypatch):
    real_pwrite = os.pwrite

    def full_disk(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(locks.os, "pwrite", full_disk)
    with pytest.raises(OSError, match="No space"):
        await locks.PhoneLock(UDID, mode="serve", home=tmp_path).acquire(1)
    monkeypatch.setattr(locks.os, "pwrite", real_pwrite)
    survivor = locks.PhoneLock(UDID, mode="mcp", home=tmp_path)
    await survivor.acquire(0.5)
    survivor.release()


async def test_cleaning_state_set_while_waiting_survives_taking_the_lock(tmp_path):
    holder = locks.PhoneLock(UDID, mode="mcp", home=tmp_path)
    await holder.acquire(1)
    holder.mark_cleaning()
    waiter = locks.PhoneLock(UDID, mode="mcp", home=tmp_path / "b")
    waiter.mark_cleaning()  # Its client left while it waited.
    asyncio.get_running_loop().call_later(0.2, holder.release)
    await waiter.acquire(2)
    assert json.loads(waiter.path.read_text())["state"] == "cleaning"
    waiter.release()


async def test_every_phone_this_process_holds_can_be_marked_cleaning_from_another_thread(tmp_path):
    held = locks.PhoneLock(UDID, mode="mcp", home=tmp_path)
    await held.acquire(1)
    await asyncio.to_thread(locks.PhoneLock.mark_held_cleaning)
    with pytest.raises(locks.PhoneBusy, match="still closing"):
        await locks.PhoneLock(UDID, mode="serve", home=tmp_path / "b").acquire(0.3)
    held.release()


async def test_advice_names_the_holders_own_state_directory_url_and_idle_timeout(tmp_path):
    serve = locks.PhoneLock(UDID, mode="serve", home=tmp_path / "a", url="http://127.0.0.1:9000")
    await serve.acquire(1)
    with pytest.raises(locks.PhoneBusy) as caught:
        await locks.PhoneLock(UDID, mode="mcp", home=tmp_path / "b").acquire(1)
    assert f"--home {tmp_path / 'a'} call shutdown --url http://127.0.0.1:9000" in str(caught.value)
    serve.release()
    for idle, expected in ((0, "never disconnects on its own"), (600, "10 minutes")):
        mcp = locks.PhoneLock(UDID, mode="mcp", home=tmp_path / "a", idle_timeout=idle)
        await mcp.acquire(1)
        with pytest.raises(locks.PhoneBusy, match=expected):
            await locks.PhoneLock(UDID, mode="serve", home=tmp_path / "b").acquire(1)
        mcp.release()


def test_lock_file_that_is_a_link_is_not_followed(tmp_path, private_phone_locks):
    target = tmp_path / "elsewhere"
    target.write_text("owner data")
    lock = locks.PhoneLock(UDID, mode="serve", home=tmp_path)
    lock.path.symlink_to(target)
    with pytest.raises(OSError):
        asyncio.run(lock.acquire(0.1))
    assert target.read_text() == "owner data"
