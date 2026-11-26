"""The real executable, killed by every route a client has, still finishes its cleanup."""

import asyncio
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest
from mcp import ClientSession
from mcp.client.stdio import PROCESS_TERMINATION_TIMEOUT, StdioServerParameters, stdio_client

from iphone_agent import cli, mcp_session
from iphone_agent.audit import AuditLog
from iphone_agent.core import ConnectionUnavailable

UDID = "00000000-FAKE-PHONE"
EXECUTABLE = str(Path(sys.executable).parent / "iphone-agent")
FAKE = str(Path(__file__).parent / "fake_phone_session")
WAIT = float(os.environ.get("IPHONE_AGENT_TEST_WAIT", 60))


def server_args(home, *extra):
    return ["--home", str(home), "mcp", "--udid", UDID, *extra]


def server_env(**extra):
    inherited = os.environ.get("PYTHONPATH")
    return {**os.environ, "PYTHONPATH": FAKE + (os.pathsep + inherited if inherited else ""), **extra}


def message(request_id, method, params):
    return (json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n").encode()


INITIALIZE = message(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "0"}})
INITIALIZED = (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode()


async def wait_until(predicate, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.01)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def processes():
    listed = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, check=True).stdout
    return [(int(pid), int(ppid), command) for pid, ppid, command in
            (line.split(None, 2) for line in listed.splitlines() if len(line.split(None, 2)) == 3)]


def worker_pids(home):
    return {pid for pid, _, command in processes() if f"--home {home} " in command and "--session-worker" in command}


def session_pid(home, server, before=frozenset()):
    """The process that owns the phone: the worker, or the server itself if there were none."""
    started = worker_pids(home) - before
    assert len(started) <= 1
    return started.pop() if started else server


def events(home):
    path = home / "events.log"
    return path.read_text().splitlines() if path.exists() else []


def has_event(home, name, *, pid=None):
    return any(line.split(" ", 1)[1].startswith(name) and (pid is None or line.startswith(f"{pid} "))
               for line in events(home))


class Client:
    """A stdio client that owns the server the way the SDK does: pipes and a new process group."""

    def __init__(self, home):
        self.home = home
        self.process = None
        self.worker = None
        self.received = []

    async def start(self, *extra, pass_fds=(), **env):
        before = worker_pids(self.home)
        self.process = await asyncio.create_subprocess_exec(
            EXECUTABLE, *server_args(self.home, *extra), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=server_env(**env), start_new_session=True, pass_fds=pass_fds)
        await self.send(INITIALIZE)
        assert "result" in await self.reply(1)
        await self.send(INITIALIZED)
        self.worker = session_pid(self.home, self.process.pid, before)
        return self

    async def send(self, data):
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def reply(self, request_id, timeout=WAIT):
        async def read():
            while True:
                line = await self.process.stdout.readline()
                assert line, "server closed stdout"
                self.received.append(line)
                value = json.loads(line)
                if value.get("id") == request_id:
                    return value
        return await asyncio.wait_for(read(), timeout)

    async def call(self, request_id, tool="iphone_capabilities", arguments=None, timeout=WAIT):
        await self.send(message(request_id, "tools/call", {"name": tool, "arguments": arguments or {}}))
        return (await self.reply(request_id, timeout))["result"]

    async def connect_phone(self):
        result = await self.call(2)
        assert not result.get("isError"), result
        assert has_event(self.home, "connected", pid=self.worker)
        assert has_event(self.home, "nudge 0x07 0x68", pid=self.worker)  # The keeper sent its first nudge.
        return self

    async def pipes_closed(self):
        """The server's pipes reach EOF, so the worker holds none of them; returns its stdout."""
        rest, _ = await asyncio.wait_for(self.process.communicate(), WAIT)
        return b"".join(self.received) + rest


@pytest.fixture
async def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    yield path
    (path / "release").touch()
    (path / "release-reply").touch()
    for pid in worker_pids(path):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
            await wait_until(lambda: not alive(pid))
            os.kill(pid, 0)
    for line in events(path):
        if line.split(" ", 2)[1] == "child" and alive(int(line.split()[2])):
            os.kill(int(line.split()[2]), signal.SIGKILL)


async def assert_cleanup_outlives_kill(client):
    """Cleanup is running past the kill, then finishes and resolves everything once allowed."""
    home, worker = client.home, client.worker
    await wait_until(lambda: has_event(home, "cleanup-writes-ok", pid=worker))
    assert alive(worker) and not has_event(home, "cleanup-finished", pid=worker)
    (home / "release").touch()
    await wait_until(lambda: has_event(home, "cleanup-finished", pid=worker))
    await wait_until(lambda: not alive(worker))
    recorded = [record["event"] for record in AuditLog.verify(home / "audit.jsonl")]
    assert recorded[-2:] == ["keep_awake", "connection_cleanup"]


def assert_only_protocol_on_stdout(stdout):
    for line in stdout.splitlines():
        assert json.loads(line)["jsonrpc"] == "2.0"
    assert b"STRAY" not in stdout


def signal_group(pgid, number):
    try:
        os.killpg(pgid, number)
    except ProcessLookupError:
        pass


# Route 1: stdin EOF, then SIGTERM and SIGKILL to the process group, as the SDK schedules them.

async def test_group_kill_on_the_sdk_schedule_leaves_cleanup_running(home):
    client = await (await Client(home).start()).connect_phone()
    pgid = client.process.pid
    client.process.stdin.close()
    # A server that has not exited after each grace period gets the next signal.
    try:
        await asyncio.wait_for(client.process.wait(), PROCESS_TERMINATION_TIMEOUT)
    except TimeoutError:
        signal_group(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(client.process.wait(), PROCESS_TERMINATION_TIMEOUT)
        except TimeoutError:
            signal_group(pgid, signal.SIGKILL)
    await asyncio.wait_for(client.process.wait(), WAIT)
    signal_group(pgid, signal.SIGTERM)
    signal_group(pgid, signal.SIGKILL)
    assert client.process.returncode == 0  # It left in the first grace period, without looking like a failure.
    assert_only_protocol_on_stdout(await client.pipes_closed())
    await assert_cleanup_outlives_kill(client)


async def test_immediate_group_sigkill_with_stdin_open_leaves_cleanup_running(home):
    client = await (await Client(home).start()).connect_phone()
    signal_group(client.process.pid, signal.SIGKILL)
    assert_only_protocol_on_stdout(await client.pipes_closed())
    await assert_cleanup_outlives_kill(client)


async def test_sigterm_with_stdin_open_exits_zero_and_leaves_cleanup_running(home):
    client = await (await Client(home).start()).connect_phone()
    signal_group(client.process.pid, signal.SIGTERM)
    assert await asyncio.wait_for(client.process.wait(), PROCESS_TERMINATION_TIMEOUT) == 0
    await assert_cleanup_outlives_kill(client)


async def test_real_sdk_client_sees_the_server_exit_within_its_first_grace_period(home):
    params = StdioServerParameters(command=EXECUTABLE, args=server_args(home), env={
        "PYTHONPATH": server_env()["PYTHONPATH"], "IPHONE_AGENT_LOCK_DIR": os.environ["IPHONE_AGENT_LOCK_DIR"]})
    with open(home.parent / "client-stderr", "w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write), ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), WAIT)
            result = await asyncio.wait_for(session.call_tool("iphone_state", {}), WAIT)
            assert not result.is_error
            worker = int(json.loads(result.content[0].text)["pid"])
            closing = time.monotonic()
    assert time.monotonic() - closing < PROCESS_TERMINATION_TIMEOUT
    await assert_cleanup_outlives_kill(SimpleNamespace(home=home, worker=worker))
    assert "STRAY" not in (home.parent / "client-stderr").read_text()


# Route 3: a client that walks descendants by parent PID and kills them all.

async def test_tree_kill_by_parent_pid_cannot_reach_the_worker(home):
    client = await (await Client(home).start()).connect_phone()
    table = processes()
    tree, frontier = {client.process.pid}, {client.process.pid}
    while frontier:
        frontier = {pid for pid, ppid, _ in table if ppid in frontier} - tree
        tree |= frontier
    assert client.worker not in tree
    assert next(ppid for pid, ppid, _ in table if pid == client.worker) == 1
    assert os.getsid(client.worker) != os.getsid(client.process.pid)
    for pid in tree:
        os.kill(pid, signal.SIGKILL)
    assert_only_protocol_on_stdout(await client.pipes_closed())
    await assert_cleanup_outlives_kill(client)


# Route 4: the client itself dies, so every pipe closes.

CLIENT_SCRIPT = textwrap.dedent("""\
    import json, subprocess, sys
    server = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def rpc(i, method, params):
        server.stdin.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\\n").encode())
        server.stdin.flush()
        while json.loads(server.stdout.readline()).get("id") != i:
            pass
    rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    server.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\\n").encode())
    rpc(2, "tools/call", {"name": "iphone_capabilities", "arguments": {}})
    print(server.pid, flush=True)
    sys.stdin.read()
    """)


async def spawn_client_script(home):
    client = await asyncio.create_subprocess_exec(
        sys.executable, "-c", CLIENT_SCRIPT, EXECUTABLE, *server_args(home), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, env=server_env(), start_new_session=True)
    server = int(await asyncio.wait_for(client.stdout.readline(), WAIT))
    return client, server


async def test_client_death_closes_every_pipe_and_leaves_cleanup_running(home):
    client, server = await spawn_client_script(home)
    worker = session_pid(home, server)
    client.kill()
    await asyncio.wait_for(client.wait(), WAIT)
    await wait_until(lambda: not alive(server))
    await assert_cleanup_outlives_kill(SimpleNamespace(home=home, worker=worker))


async def test_client_death_with_its_pipes_held_open_elsewhere_still_ends_the_session(home):
    # Another process inherited the client's end of stdin, so the server never sees EOF.
    holder_script = CLIENT_SCRIPT.replace(
        "stderr=subprocess.PIPE)",
        "stderr=subprocess.PIPE)\n"
        "holder = subprocess.Popen(['/bin/sleep', '3600'], pass_fds=[server.stdin.fileno()], start_new_session=True,\n"
        "                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(holder.pid, flush=True)")
    client = await asyncio.create_subprocess_exec(
        sys.executable, "-c", holder_script, EXECUTABLE, *server_args(home), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, env=server_env(), start_new_session=True)
    holder = int(await asyncio.wait_for(client.stdout.readline(), WAIT))
    try:
        server = int(await asyncio.wait_for(client.stdout.readline(), WAIT))
        worker = session_pid(home, server)
        client.kill()
        await asyncio.wait_for(client.wait(), WAIT)
        await wait_until(lambda: not alive(server))
        await assert_cleanup_outlives_kill(SimpleNamespace(home=home, worker=worker))
    finally:
        os.kill(holder, signal.SIGKILL)


# Writes during cleanup, once nothing is left to read them.

async def test_cleanup_writes_find_no_broken_pipe_and_sigpipe_cannot_kill_it(home):
    client = await (await Client(home).start()).connect_phone()
    client.process.kill()
    assert_only_protocol_on_stdout(await client.pipes_closed())
    await wait_until(lambda: has_event(home, "cleanup-writes-ok", pid=client.worker))
    assert any("cleanup-started sigpipe=" in line and "SIG_IGN" in line for line in events(home))
    log = (home / mcp_session.LOG_NAME).read_text()
    for stray in ("STRAY stdout print", "STRAY stderr print", "STRAY logging", "STRAY fd 1", "STRAY fd 2",
                  "STRAY rich stdout", "STRAY rich stderr"):
        assert stray in log
    await assert_cleanup_outlives_kill(client)


async def test_reply_on_a_broken_relay_does_not_stop_cleanup_or_count_as_a_failure(home):
    client = await (await Client(home).start()).connect_phone()
    await client.send(message(3, "tools/call", {"name": "iphone_action", "arguments": {"action": "break_relay"}}))
    await wait_until(lambda: has_event(home, "execute break_relay"))
    await assert_cleanup_outlives_kill(client)
    log = (home / mcp_session.LOG_NAME).read_text()
    assert f"pid={client.worker} session ended with code 0" in log
    assert "BrokenPipeError" not in log


async def test_call_in_flight_when_the_client_dies_does_not_hold_up_cleanup(home):
    client = await (await Client(home).start()).connect_phone()
    await client.send(message(3, "tools/call", {"name": "iphone_action", "arguments": {"action": "slow_reply"}}))
    await wait_until(lambda: has_event(home, "execute slow_reply"))
    client.process.kill()
    await client.pipes_closed()
    await assert_cleanup_outlives_kill(client)  # Never releases the call.
    assert f"pid={client.worker} session ended with code 0" in (home / mcp_session.LOG_NAME).read_text()


# Sessions that never touched the phone, or were still reaching for it.

@pytest.mark.parametrize("route", ["eof", "sigkill"])
async def test_idle_session_leaves_nothing_behind_and_never_touches_the_phone(home, route):
    client = await Client(home).start()
    if route == "eof":
        client.process.stdin.close()
    else:
        client.process.kill()
    await client.pipes_closed()
    await wait_until(lambda: not alive(client.worker))
    assert events(home) == []
    assert not (home / mcp_session.LOCK_NAME).exists()
    assert not (home / "audit.jsonl").exists()


async def test_killed_while_the_first_call_is_still_connecting_stops_connecting(home):
    client = await Client(home).start(FAKE_STARTUP="hang")
    await client.send(message(2, "tools/call", {"name": "iphone_capabilities", "arguments": {}}))
    await wait_until(lambda: has_event(home, "start", pid=client.worker))
    client.process.kill()
    await client.pipes_closed()
    await wait_until(lambda: has_event(home, "startup-cancelled", pid=client.worker))
    await wait_until(lambda: not alive(client.worker))


async def test_stdin_eof_while_the_first_call_is_still_connecting_stops_connecting(home):
    client = await Client(home).start(FAKE_STARTUP="hang")
    await client.send(message(2, "tools/call", {"name": "iphone_capabilities", "arguments": {}}))
    await wait_until(lambda: has_event(home, "start", pid=client.worker))
    client.process.stdin.close()
    assert await asyncio.wait_for(client.process.wait(), PROCESS_TERMINATION_TIMEOUT) == 0
    await wait_until(lambda: has_event(home, "startup-cancelled", pid=client.worker))
    await wait_until(lambda: not alive(client.worker))


# A new client session that starts while the old one is cleaning up.

async def test_first_call_waits_for_the_old_cleanup(home):
    old = await (await Client(home).start()).connect_phone()
    old.process.kill()
    await old.pipes_closed()
    await wait_until(lambda: has_event(home, "cleanup-writes-ok", pid=old.worker))

    new = await Client(home).start("--startup-timeout", "300")
    await new.send(message(2, "tools/call", {"name": "iphone_capabilities", "arguments": {}}))
    log = home / mcp_session.LOG_NAME
    await wait_until(lambda: f"pid={new.worker} waiting for previous session pid={old.worker}" in log.read_text())
    assert not has_event(home, "start", pid=new.worker)
    (home / "release").touch()
    assert not (await new.reply(2))["result"].get("isError")

    order = events(home)
    finished = order.index(f"{old.worker} cleanup-finished")
    assert order.index(next(line for line in order if line.startswith(f"{new.worker} start"))) > finished
    # The wait shortens the first connect only: reconnects still get the whole --startup-timeout.
    start = next(line for line in order if line.startswith(f"{new.worker} start"))
    assert "timeout=300 " in start
    assert 0 < float(start.split("first=")[1]) < 300

    new.process.stdin.close()
    await new.pipes_closed()
    await wait_until(lambda: has_event(home, "cleanup-finished", pid=new.worker))
    AuditLog.verify(home / "audit.jsonl")


async def test_first_call_gets_a_clear_retryable_error_when_cleanup_outlasts_the_startup_timeout(home):
    old = await (await Client(home).start()).connect_phone()
    old.process.kill()
    await old.pipes_closed()
    await wait_until(lambda: has_event(home, "cleanup-writes-ok", pid=old.worker))

    new = await Client(home).start("--startup-timeout", "0.5")
    result = await new.call(2)
    assert result["isError"]
    text = result["content"][0]["text"]
    assert f"previous iPhone session (pid {old.worker}) is still closing its connection" in text
    assert "Retry the call" in text
    assert not has_event(home, "start", pid=new.worker)

    (home / "release").touch()
    await wait_until(lambda: not alive(old.worker))
    assert not (await new.call(3)).get("isError")
    new.process.stdin.close()
    await new.pipes_closed()
    await wait_until(lambda: not alive(new.worker))
    assert has_event(home, "cleanup-finished", pid=new.worker)


async def test_second_live_session_is_refused_at_once_without_touching_the_phone(home):
    first = await (await Client(home).start()).connect_phone()
    second = await Client(home).start("--startup-timeout", "300")
    result = await second.call(2, timeout=WAIT)
    assert result["isError"]
    assert f"Another iPhone session (pid {first.worker})" in result["content"][0]["text"]
    assert not has_event(home, "start", pid=second.worker)
    second.process.stdin.close()
    first.process.stdin.close()
    await wait_until(lambda: not alive(second.worker))
    await assert_cleanup_outlives_kill(first)


async def test_lock_is_free_once_its_holder_is_killed(home):
    client = await (await Client(home).start()).connect_phone()
    os.kill(client.worker, signal.SIGKILL)
    await wait_until(lambda: not alive(client.worker))
    lock = mcp_session.SessionLock(home)
    assert await lock.acquire(0) is False
    lock.release()


# The background process always ends, and says what went wrong.

async def test_hung_cleanup_is_ended_at_the_bound_with_its_children_and_is_logged(home):
    client = await Client(home).start(FAKE_CLEANUP="hang", FAKE_CLEANUP_LIMIT="1")
    await client.connect_phone()
    client.process.kill()
    await client.pipes_closed()
    await wait_until(lambda: has_event(home, "child", pid=client.worker))
    child = next(int(line.split()[2]) for line in events(home) if line.split(" ", 2)[1] == "child")
    await wait_until(lambda: not alive(client.worker))
    assert not alive(child)
    log = (home / mcp_session.LOG_NAME).read_text()
    assert "cleanup exceeded 1s; forcing exit" in log and f"pid={client.worker} session ended with code 3" in log


async def test_stuck_event_loop_cannot_outlive_the_bound_once_the_client_is_gone(home):
    client = await Client(home).start(FAKE_CLEANUP_LIMIT="1")
    await client.connect_phone()
    await client.send(message(3, "tools/call", {"name": "iphone_action", "arguments": {"action": "wedge"}}))
    await wait_until(lambda: has_event(home, "execute wedge"))
    client.process.kill()
    await client.pipes_closed()
    await wait_until(lambda: not alive(client.worker))
    log = (home / mcp_session.LOG_NAME).read_text()
    assert "cleanup exceeded 1s; forcing exit" in log and f"pid={client.worker} session ended with code 3" in log


async def test_cleanup_failure_lands_in_the_private_log(home):
    client = await Client(home).start(FAKE_CLEANUP="fail")
    await client.connect_phone()
    client.process.kill()
    await client.pipes_closed()
    await wait_until(lambda: not alive(client.worker))
    log = home / mcp_session.LOG_NAME
    assert "RuntimeError: cleanup failed on purpose" in log.read_text()
    assert f"pid={client.worker} session ended with code 1" in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600


async def test_worker_that_cannot_start_is_a_visible_failure_not_a_hang(home):
    process = await asyncio.create_subprocess_exec(
        EXECUTABLE, *server_args(home), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=server_env(FAKE_WORKER_CRASH="1"), start_new_session=True)
    # Stdin stays open: this server has to notice by itself that its worker is gone.
    stderr = await asyncio.wait_for(process.stderr.read(), WAIT)
    assert await asyncio.wait_for(process.wait(), WAIT) == 1
    assert await process.stdout.read() == b""
    assert mcp_session.LOG_NAME.encode() in stderr


# In-process details.

async def test_broken_stderr_cannot_stop_the_exit_after_cleanup(monkeypatch):
    class Broken:
        def write(self, _):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

    async def start(args, hold, **options):
        await hold(SimpleNamespace(execute=None))
        raise RuntimeError("cleanup failed")

    exited = asyncio.get_running_loop().create_future()

    async def serve(host):
        await host._ensure()
        monkeypatch.setattr(sys, "stderr", Broken())
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.Event().wait()

    task = asyncio.create_task(cli.run_mcp_deferred(SimpleNamespace(), serve=serve, exit=exited.set_result,
                                                    host=cli.DeferredHost(SimpleNamespace(), start=start)))
    assert await asyncio.wait_for(exited, 5) == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_session_busy_reaches_the_caller_with_its_own_text():
    async def start(args, hold, **options):
        raise mcp_session.SessionBusy("still closing. Retry the call in a minute.")

    host = cli.DeferredHost(SimpleNamespace(), start=start)
    with pytest.raises(ConnectionUnavailable, match=r"^still closing\. Retry the call in a minute\.$"):
        await host.execute("state")
    await host.close()


async def test_close_tells_its_listener_once():
    calls = []
    host = cli.DeferredHost(SimpleNamespace(), start=None, on_close=lambda: calls.append(1))
    await host.close()
    await host.close()
    assert calls == [1]


def test_worker_flag_is_mcp_only(tmp_path):
    # With the fake links and a private home, a serve that accepted the flag could only time out here.
    completed = subprocess.run([EXECUTABLE, "--home", str(tmp_path), "serve", "--session-worker", "9"],
                               env=server_env(FAKE_TRANSPORT="1"), capture_output=True, text=True, timeout=WAIT)
    assert completed.returncode == 2 and "unrecognized arguments" in completed.stderr


# The real connect and reconnect paths, with only the links to the phone replaced.

def lines_with(home, text):
    return [line for line in events(home) if text in line]


def log_of(home):
    return (home / mcp_session.LOG_NAME).read_text()


def audited(home):
    return [record["event"] for record in AuditLog.verify(home / "audit.jsonl")]


def keep_awake_operations(home):
    return [record["operation"] for record in AuditLog.verify(home / "audit.jsonl") if record["event"] == "keep_awake"]


def tool_result(result):
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


async def linked_client(home, **env):
    client = await Client(home).start(FAKE_TRANSPORT="1", **env)
    tool_result(await client.call(2))
    return client


async def ask_reconnect(client):
    await client.send(message(3, "tools/call", {"name": "iphone_action", "arguments": {"action": "reconnect"}}))


async def session_ends(client):
    client.process.kill()
    await client.pipes_closed()
    await wait_until(lambda: not alive(client.worker))


async def test_connect_nudges_the_idle_timer_without_navigating_the_phone(home):
    client = await linked_client(home)
    capabilities = tool_result(await client.call(3))
    assert capabilities["locked"] is False
    assert capabilities["keepAwake"]["method"] == "input-nudge" and capabilities["keepAwake"]["changesSettings"] is False
    assert capabilities["keepAwake"]["active"] is True and capabilities["keepAwake"]["lastError"] is None
    assert lines_with(home, "nudge 0x07 0x68")
    assert lines_with(home, "dispatch ") == []  # No deep_link, tap or tap_element went to the phone.
    assert keep_awake_operations(home) == ["start"]
    await session_ends(client)
    assert keep_awake_operations(home) == ["start", "stop"]
    assert f"pid={client.worker} session ended with code 0" in log_of(home)


async def test_locked_phone_connects_at_once_refuses_input_and_still_reads_the_screen(home):
    (home / "locked").touch()
    client = await Client(home).start(FAKE_TRANSPORT="1")
    began = time.monotonic()
    capabilities = tool_result(await client.call(2))
    assert time.monotonic() - began < WAIT / 2
    assert capabilities["locked"] is True and capabilities["keepAwake"]["locked"] is True
    assert lines_with(home, "nudge ") == []
    refused = await client.call(3, "iphone_tap", {"x": 10, "y": 20})
    assert refused.get("isError") and "locked" in refused["content"][0]["text"]
    assert lines_with(home, "dispatch ") == []
    assert not (await client.call(4, "iphone_state")).get("isError")
    assert lines_with(home, "dispatch ") == [f"{client.worker} dispatch state"]
    (home / "locked").unlink()
    assert not (await client.call(5, "iphone_tap", {"x": 10, "y": 20})).get("isError")
    assert lines_with(home, "dispatch tap")
    await session_ends(client)


async def test_no_keep_awake_session_sends_no_nudge(home):
    client = await Client(home).start("--no-keep-awake", FAKE_TRANSPORT="1")
    assert tool_result(await client.call(2))["keepAwake"] == {"active": False}
    await session_ends(client)
    assert lines_with(home, "nudge ") == [] and len(lines_with(home, "connect ")) == 1
    assert keep_awake_operations(home) == []


async def test_finished_reconnect_gives_the_new_connection_its_own_keeper(home):
    client = await linked_client(home)
    await ask_reconnect(client)
    assert not (await client.reply(3))["result"].get("isError")
    await session_ends(client)
    assert len(lines_with(home, "connect ")) == 2
    assert keep_awake_operations(home) == ["start", "stop", "start", "stop"]
    assert f"pid={client.worker} session ended with code 0" in log_of(home)


async def test_client_leaving_while_the_reconnect_connects_ends_without_another_connection(home):
    client = await linked_client(home, FAKE_HOLD="2:connect")
    await ask_reconnect(client)
    await wait_until(lambda: has_event(home, "holding 2:connect"))
    await session_ends(client)
    assert lines_with(home, "connect ")[-1].endswith("connect 2")
    assert f"pid={client.worker} session ended with code 0" in log_of(home)
    assert "connection_cleanup" in audited(home)


def assert_call_reports_the_lost_link(result):
    text = result["content"][0]["text"]
    assert result.get("isError") and "the link to the phone is gone" in text and "cleanup failed" not in text, result


async def test_link_lost_during_the_first_connect_answers_the_call_and_leaving_connects_no_more(home):
    (home / "dead").touch()
    client = await Client(home).start(FAKE_TRANSPORT="1")
    began = time.monotonic()
    assert_call_reports_the_lost_link(await client.call(2))
    assert time.monotonic() - began < WAIT / 2
    (home / "dead").unlink()
    await session_ends(client)
    assert len(lines_with(home, "connect ")) == 1
    assert lines_with(home, "nudge ") == []


async def test_retried_call_after_a_lost_link_connects(home):
    (home / "dead").touch()
    client = await Client(home).start(FAKE_TRANSPORT="1")
    assert_call_reports_the_lost_link(await client.call(2))
    (home / "dead").unlink()
    tool_result(await client.call(3))
    await session_ends(client)
    assert len(lines_with(home, "connect ")) == 2
    assert keep_awake_operations(home) == ["start", "stop"]


# Details of the client's own process and pipes.

async def test_descriptors_the_client_passed_down_do_not_stay_open_in_the_worker(home):
    read_end, write_end = os.pipe()
    os.set_blocking(read_end, False)
    try:
        client = await (await Client(home).start(pass_fds=(write_end,))).connect_phone()
        os.close(write_end)
        write_end = None
        client.process.kill()
        await client.pipes_closed()
        assert alive(client.worker) and not has_event(home, "cleanup-finished", pid=client.worker)

        def at_eof():
            try:
                return os.read(read_end, 1) == b""
            except BlockingIOError:
                return False
        await wait_until(at_eof)
        assert alive(client.worker)  # Cleanup still waits for its release, and did not hold the pipe.
    finally:
        os.close(read_end)
        if write_end is not None:
            os.close(write_end)


async def test_non_blocking_client_stdio_does_not_end_the_session(home):
    stdin_read, stdin_write = os.pipe()
    stdout_read, stdout_write = os.pipe()
    os.set_blocking(stdin_read, False)
    os.set_blocking(stdout_write, False)
    process = await asyncio.create_subprocess_exec(
        EXECUTABLE, *server_args(home), stdin=stdin_read, stdout=stdout_write, stderr=asyncio.subprocess.DEVNULL,
        env=server_env(), start_new_session=True)
    os.close(stdin_read)
    os.close(stdout_write)
    try:
        await wait_until(lambda: worker_pids(home))
        await asyncio.sleep(0.5)  # Nothing to read yet: the relay's first read on stdin finds an empty pipe.
        assert process.returncode is None
        os.write(stdin_write, INITIALIZE + INITIALIZED +
                 message(2, "tools/call", {"name": "iphone_action", "arguments": {"action": "big_reply"}}))
        await wait_until(lambda: has_event(home, "execute big_reply"))
        await asyncio.sleep(1.0)  # The reply outgrows the pipe while nobody reads it.
        assert process.returncode is None

        def read_all_replies():
            data = bytearray()
            while b'"id":2' not in data or not data.endswith(b"\n"):
                chunk = os.read(stdout_read, 65536)
                assert chunk, "server closed stdout"
                data += chunk
            return data
        data = await asyncio.wait_for(asyncio.to_thread(read_all_replies), WAIT)
        reply = next(value for value in map(json.loads, data.splitlines()) if value.get("id") == 2)
        assert not reply["result"].get("isError") and len(json.dumps(reply)) > 200_000
    finally:
        os.close(stdin_write)
        await asyncio.wait_for(process.wait(), WAIT)
        os.close(stdout_read)
        (home / "release").touch()


async def test_cold_worker_still_answers_a_one_shot_piped_client(home):
    process = await asyncio.create_subprocess_exec(
        EXECUTABLE, *server_args(home), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=server_env(FAKE_WORKER_GATE="1"), start_new_session=True)
    process.stdin.write(INITIALIZE)
    await process.stdin.drain()
    process.stdin.close()
    await asyncio.sleep(2.0)  # Longer than the old limit on waiting for replies, while the worker imports.
    assert process.returncode is None
    (home / "release-worker").touch()
    stdout, _ = await asyncio.wait_for(process.communicate(), WAIT)
    assert process.returncode == 0
    assert "result" in json.loads(stdout.splitlines()[0])


async def test_another_controller_holding_the_audit_journal_gets_a_clear_retryable_error(home):
    holder = AuditLog(home / "audit.jsonl")
    client = await Client(home).start()
    try:
        result = await client.call(2)
        assert result.get("isError")
        text = result["content"][0]["text"]
        assert "Another iphone-agent controller" in text and "Connect it by USB" not in text
    finally:
        holder.close()
    assert not (await client.call(3)).get("isError")
    assert has_event(home, "connected", pid=client.worker)
    client.process.stdin.close()
    await client.pipes_closed()


async def test_failed_connect_releases_the_session_lock_for_the_next_call(home):
    client = await Client(home).start(FAKE_STARTUP="fail-once")
    assert (await client.call(2)).get("isError")
    # A lock still held here would make this worker see itself as the busy session.
    assert not (await client.call(3, timeout=10)).get("isError")
    assert has_event(home, "connected", pid=client.worker)
    client.process.stdin.close()
    await client.pipes_closed()


async def test_a_long_session_rotates_its_own_log(home):
    client = await Client(home).start(FAKE_LOG_LIMIT="4096")
    result = await client.call(2, "iphone_action", {"action": "log_lines"})
    assert not result.get("isError"), result
    log = home / mcp_session.LOG_NAME
    await wait_until(lambda: "AFTER-THE-LINES" in log.read_text() or
                     "AFTER-THE-LINES" in log.with_name(mcp_session.LOG_NAME + ".1").read_text())
    await wait_until(lambda: log.stat().st_size <= 4096)
    assert log.with_name(mcp_session.LOG_NAME + ".1").exists()
    await asyncio.sleep(0.2)
    assert log.stat().st_size <= 4096  # New lines land in the new file, not the rotated one.

    rotated = log.with_name(mcp_session.LOG_NAME + ".1")
    async def marks_land_in_the_new_file():
        # The check that rotated the log moves the descriptors just after it, so ask until both have moved.
        for mark in range(1000):
            result = await client.call(10 + mark, "iphone_action", {"action": "log_marks", "params": {"mark": mark}})
            assert not result.get("isError"), result
            text = log.read_text()
            if f"MARK-FD1-{mark}\n" in text and f"MARK-FD2-{mark}\n" in text:
                return mark
            await asyncio.sleep(0.05)
    mark = await asyncio.wait_for(marks_land_in_the_new_file(), WAIT)
    assert f"MARK-FD1-{mark}\n" not in rotated.read_text() and f"MARK-FD2-{mark}\n" not in rotated.read_text()
    client.process.stdin.close()
    await client.pipes_closed()


async def test_log_checks_leave_the_descriptors_alone_until_the_log_rotates(home):
    calls = home / "dup2.calls"
    client = await Client(home).start(FAKE_LOG_LIMIT="1048576", FAKE_DUP2_LOG=str(calls))
    result = await client.call(2)
    assert not result.get("isError"), result
    await asyncio.sleep(0.2)  # Serving has claimed the descriptors by now.
    before = calls.read_text()
    await asyncio.sleep(0.3)  # Six log checks.
    assert calls.read_text() == before
    client.process.stdin.close()
    await client.pipes_closed()


async def test_log_checks_do_not_leak_descriptors(home):
    client = await Client(home).start(FAKE_LOG_LIMIT="1048576")

    async def open_descriptors(call):
        result = await client.call(call, "iphone_action", {"action": "open_fds"})
        return json.loads(result["content"][0]["text"])["result"]["open"]

    before = await open_descriptors(2)
    await asyncio.sleep(0.5)  # Ten log checks.
    assert await open_descriptors(3) - before < 3
    client.process.stdin.close()
    await client.pipes_closed()


async def test_a_fault_in_a_log_check_leaves_the_checks_running(home):
    client = await Client(home).start(FAKE_LOG_LIMIT="4096", FAKE_LOG_FAULT="1")
    await wait_until(lambda: "log check failed, trying again at the next one" in log_of(home))
    result = await client.call(2, "iphone_action", {"action": "log_lines"})
    assert not result.get("isError"), result
    await wait_until(lambda: (home / (mcp_session.LOG_NAME + ".1")).exists())
    client.process.stdin.close()
    await client.pipes_closed()


async def test_log_check_before_the_sdk_claims_fd_1_leaves_the_relay_in_place(home):
    client = await Client(home).start(FAKE_LOG_LIMIT="4096", FAKE_SERVE_DELAY="0.3")
    result = await client.call(2)
    assert not result.get("isError"), result
    client.process.stdin.close()
    await client.pipes_closed()


def test_a_worker_rotating_the_log_leaves_the_rotation_to_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_session, "LOG_ROTATE_BYTES", 10)
    log = tmp_path / mcp_session.LOG_NAME
    rotated = tmp_path / (mcp_session.LOG_NAME + ".1")
    log.write_text("the log so far\n")
    rotated.write_text("the older log\n")
    other_worker = os.open(tmp_path, os.O_RDONLY)
    try:
        fcntl.flock(other_worker, fcntl.LOCK_EX)
        os.close(mcp_session._open_log(tmp_path))
        assert log.read_text() == "the log so far\n" and rotated.read_text() == "the older log\n"
    finally:
        os.close(other_worker)
    os.close(mcp_session._open_log(tmp_path))
    assert rotated.read_text() == "the log so far\n" and log.read_text() == ""


def wrote_runner_config(home):
    (home / "runner.json").write_text(json.dumps({"udid": UDID, "bundleId": "com.fake.runner.xctrunner",
                                                   "appPath": str(home / "app")}))


async def test_cable_lost_during_the_wda_wait_stops_the_real_runner_and_moves_to_the_network(home):
    wrote_runner_config(home)
    (home / "usb").touch()
    client = await Client(home).start("--network-fallback", FAKE_TRANSPORT="1", FAKE_RUNNER="1", FAKE_HOME=str(home),
                                      PATH=f"{FAKE}/fakebin{os.pathsep}{os.environ['PATH']}")
    await client.send(message(2, "tools/call", {"name": "iphone_capabilities", "arguments": {}}))
    await wait_until(lambda: (home / "xcodebuild.pid").exists() and (home / "xcodebuild.child").exists())
    runner, child = int((home / "xcodebuild.pid").read_text()), int((home / "xcodebuild.child").read_text())
    try:
        assert alive(runner) and alive(child) and list(home.glob("wda-launch-*.xctestrun"))
        (home / "usb").unlink()
        result = (await client.reply(2))["result"]
        assert not result.get("isError"), result
        assert json.loads(result["content"][0]["text"])["transport"] == "paired-network"
        await wait_until(lambda: not alive(runner) and not alive(child))
        assert not list(home.glob("wda-launch-*.xctestrun"))
        assert "the USB link disappeared" in " ".join(log_of(home).split())
    finally:
        for pid in (runner, child):
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
    await session_ends(client)


async def test_cable_staying_up_lets_the_real_runner_finish_on_usb(home):
    wrote_runner_config(home)
    (home / "usb").touch()
    client = await Client(home).start("--network-fallback", FAKE_TRANSPORT="1", FAKE_RUNNER="1", FAKE_HOME=str(home),
                                      PATH=f"{FAKE}/fakebin{os.pathsep}{os.environ['PATH']}")
    await client.send(message(2, "tools/call", {"name": "iphone_capabilities", "arguments": {}}))
    await wait_until(lambda: (home / "xcodebuild.pid").exists() and (home / "xcodebuild.child").exists())
    runner, child = int((home / "xcodebuild.pid").read_text()), int((home / "xcodebuild.child").read_text())
    try:
        await asyncio.sleep(0.3)  # Several rechecks pass.
        assert alive(runner) and alive(child)
        (home / "wda-usb-ready").touch()
        result = (await client.reply(2))["result"]
        assert not result.get("isError"), result
        assert json.loads(result["content"][0]["text"])["transport"] == "USB"
        await session_ends(client)
        await wait_until(lambda: not alive(runner) and not alive(child))
    finally:
        for pid in (runner, child):
            if alive(pid):
                os.kill(pid, signal.SIGKILL)


async def test_second_state_directory_on_the_same_phone_is_refused_until_the_first_session_goes_idle(home):
    other = home.parent / "other-home"
    other.mkdir()
    first = await Client(home).start("--idle-timeout", "3", FAKE_TRANSPORT="1")
    second = await Client(other).start(FAKE_TRANSPORT="1")
    try:
        tool_result(await first.call(2))
        refused = await second.call(2)
        assert refused.get("isError"), refused
        text = refused["content"][0]["text"]
        assert f"pid {first.worker}" in text and "mcp" in text and str(home) in text and "--idle-timeout" in text
        assert lines_with(other, "connect ") == []  # The refused session never touched the phone.
        # Without calls the first session disconnects after its idle timeout, which frees the phone.
        request = 3
        while True:
            reply = await second.call(request)
            if not reply.get("isError"):
                break
            assert "in use by another iphone-agent controller" in reply["content"][0]["text"], reply
            request += 1
            await asyncio.sleep(0.5)
        assert tool_result(reply)["locked"] is False
        refused = await first.call(request + 1)
        assert refused.get("isError") and f"pid {second.worker}" in refused["content"][0]["text"]
    finally:
        (other / "release").touch()
        (other / "release-reply").touch()
        await session_ends(second)
        await session_ends(first)
