"""Let an MCP phone session close its connection after its client kills the stdio server.

Clients allow a stdio server seconds to exit, while stopping the runner's xcodebuild
and closing a paired-network connection can take longer; a server killed midway
leaves xcodebuild running. So the process a client starts only relays bytes and
exits at once, and a background worker runs the MCP server and owns the phone. The
worker is reparented to launchd in its own session, which puts it out of reach of
process-group kills and of walks down the parent PIDs. It holds none of the
client's pipes, so it cannot delay a client waiting for them to close.
"""
import asyncio
import contextlib
import fcntl
import math
import os
from pathlib import Path
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import traceback
from . import cli
from .core import ConnectionUnavailable
from .locks import HolderLock, PhoneLock

LOG_NAME = "mcp-session.log"
LOCK_NAME = "mcp-session.lock"
LOG_ROTATE_BYTES = 1024 * 1024
LOG_CHECK_INTERVAL = 30.0
RELAY_BUFFER = 65536
# The worker's replies to messages sent just before stdin closed may still be on their way, and a cold
# start took 4.91 s. A running worker closes the relay within milliseconds of the EOF, so this rarely waits.
DRAIN_TIMEOUT = 15.0
PARENT_POLL_INTERVAL = 1.0
# Above what cli.py normally spends: 30 s for each of nine resources that does not close.
CLEANUP_LIMIT = 300.0
CHILD_STOP_TIMEOUT = 2.0


class SessionBusy(ConnectionUnavailable):
    pass


def _note(message):
    with contextlib.suppress(OSError, ValueError):
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} pid={os.getpid()} {message}", file=sys.stderr, flush=True)


def _report_failure():
    with contextlib.suppress(OSError, ValueError):
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()


class SessionLock(HolderLock):
    """Allow one phone session per state directory, from connecting until all of it is closed."""

    def __init__(self, home):
        super().__init__(Path(home) / LOCK_NAME)

    def _busy(self, holder):
        return SessionBusy(f"Another iPhone session (pid {holder.get('pid')}) is using {self.path.parent}. "
                           "Close that client session or retry the call after it ends.")

    def _still_closing(self, holder, timeout):
        return SessionBusy(f"The previous iPhone session (pid {holder.get('pid')}) is still closing its "
                           f"connection after {timeout:g}s. Retry the call in a minute.")

    def _waiting(self, holder):
        _note(f"waiting for previous session pid={holder.get('pid')} to finish cleanup")


def _rotate_log(path):
    # Two workers rotating together would overwrite .1 with the nearly empty new log. One rotating is enough.
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        with contextlib.suppress(BlockingIOError, FileNotFoundError):
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if path.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(path, path.with_name(LOG_NAME + ".1"))
    finally:
        os.close(directory)


def _open_log(home):
    try:
        Path(home).mkdir(parents=True, exist_ok=True, mode=0o700)
        path = Path(home) / LOG_NAME
        _rotate_log(path)
        return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError as exc:
        # The handshake must not depend on the state directory; tool calls report it.
        os.write(2, f"iphone-agent: cannot open the session log: {exc}\n".encode())
        return os.open(os.devnull, os.O_WRONLY)


def _follow_log(fresh):
    """Move fds 2 and 1 to the open log only if it is a new file: a write during dup2 gets EBADF on macOS."""
    if not os.path.sameopenfile(fresh, 2):
        os.dup2(fresh, 2)
    # Fd 1 holds log output too, except before the SDK claims it and after it restores it: then it is the relay.
    if not stat.S_ISSOCK(os.fstat(1).st_mode) and not os.path.sameopenfile(fresh, 1):
        os.dup2(fresh, 1)


def _spawn_worker(wire, alive, log):
    """Start the worker as a grandchild: orphaned to launchd, in a session of its own."""
    command = [sys.executable, "-P", "-m", "iphone_agent", *sys.argv[1:], "--session-worker", str(alive)]
    child = os.fork()
    if child:
        os.waitpid(child, 0)
        return
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        os.dup2(wire, 0)
        os.dup2(wire, 1)
        os.dup2(log, 2)
        os.set_inheritable(alive, True)
        for fd in map(int, os.listdir("/dev/fd")):
            if fd > 2 and fd != alive:  # A pipe the client passed down must not stay open for the worker's life.
                with contextlib.suppress(OSError):
                    os.close(fd)
        os.execv(sys.executable, command)
    except BaseException:
        traceback.print_exc()
    finally:
        os._exit(127)


def run_front(args):
    """Relay the client's stdio to the worker; exit as soon as the client is done. Never returns."""
    for fd in (0, 1, 2):
        try:
            os.fstat(fd)
        except OSError:
            os.open(os.devnull, os.O_RDWR)  # Takes the lowest free number, so fd itself.
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda *_: os._exit(0))
    log = _open_log(args.home)
    front, worker = socket.socketpair()
    alive, keep_alive = os.pipe()  # The worker reads EOF the moment this process is gone.
    parent = os.getppid()
    _spawn_worker(worker.fileno(), alive, log)
    worker.close()
    os.close(alive)
    os.close(log)
    changed = threading.Event()
    stdin_closed = threading.Event()
    wire_closed = threading.Event()

    def client_to_worker():
        with contextlib.suppress(OSError):
            while True:
                try:
                    data = os.read(0, RELAY_BUFFER)
                except BlockingIOError:  # The client's end may be non-blocking.
                    select.select([0], [], [])
                    continue
                if not data:
                    break
                front.sendall(data)
        with contextlib.suppress(OSError):
            front.shutdown(socket.SHUT_WR)
        stdin_closed.set()
        changed.set()

    def worker_to_client():
        with contextlib.suppress(OSError):
            while data := front.recv(RELAY_BUFFER):
                view = memoryview(data)
                while view:
                    try:
                        view = view[os.write(1, view):]
                    except BlockingIOError:
                        select.select([], [1], [])
                    except OSError:
                        os._exit(0)  # The client stopped listening, which ends the session.
        wire_closed.set()
        changed.set()

    threading.Thread(target=client_to_worker, daemon=True).start()
    threading.Thread(target=worker_to_client, daemon=True).start()
    while True:
        changed.wait(PARENT_POLL_INTERVAL)
        changed.clear()
        if stdin_closed.is_set():
            wire_closed.wait(DRAIN_TIMEOUT)
            os._exit(0)
        if wire_closed.is_set():
            with contextlib.suppress(OSError):
                os.write(2, f"iphone-agent: the background session ended; see {Path(args.home) / LOG_NAME}\n".encode())
            os._exit(1)
        # Another process may hold the client's pipes open after the client itself is gone.
        if os.getppid() != parent:
            os._exit(0)


def _stop_children():
    """Stop what the hung session started, such as xcodebuild, which leads its own group."""
    try:
        listed = subprocess.run(["/usr/bin/pgrep", "-P", str(os.getpid())], capture_output=True, text=True,
                                timeout=CHILD_STOP_TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        _note(f"could not list child processes: {type(exc).__name__}: {exc}")
        return
    children = [int(line) for line in listed.split()]

    def signal_all(number):
        for child in children:
            with contextlib.suppress(OSError):
                os.killpg(child, number)
            with contextlib.suppress(OSError):
                os.kill(child, number)

    signal_all(signal.SIGTERM)
    time.sleep(CHILD_STOP_TIMEOUT)
    signal_all(signal.SIGKILL)


def run_worker(args):
    """Serve MCP over the relay, then clean up however the client left. Never returns."""
    for number in (signal.SIGINT, signal.SIGHUP):
        signal.signal(number, signal.SIG_IGN)
    alive = args.session_worker
    os.set_inheritable(alive, False)
    report_unraisable = sys.unraisablehook

    def drop_unsent_reply(unraisable):
        # A reply still buffered when the client left fails to flush when its writer is collected.
        # That is the client leaving, which the session handles; when it happens depends on the collector.
        if not isinstance(unraisable.exc_value, ConnectionError):
            report_unraisable(unraisable)

    sys.unraisablehook = drop_unsent_reply
    wire = socket.socket(fileno=os.dup(0))
    lock = SessionLock(args.home)
    cleanup_once = threading.Lock()
    _note("session started")

    def finish(code):
        _note(f"session ended with code {code}")
        os._exit(code)

    def force_exit():
        _note(f"cleanup exceeded {CLEANUP_LIMIT:g}s; forcing exit")
        _stop_children()
        finish(3)

    def begin_cleanup():
        if not cleanup_once.acquire(blocking=False):
            return
        for mark in (lock.mark_cleaning, PhoneLock.mark_held_cleaning):
            try:
                mark()
            except OSError as exc:  # The cleanup bound below must start regardless.
                _note(f"could not mark a lock as closing: {exc}")
        _note("cleanup started")
        bound = threading.Timer(CLEANUP_LIMIT, force_exit)
        bound.daemon = True
        bound.start()

    def watch_client():
        # The relay closing starts the cleanup itself. This starts the bound even if the event loop is stuck.
        with contextlib.suppress(OSError):
            os.read(alive, 1)
        begin_cleanup()

    def keep_log_bounded():
        # A session can run for days. Reopening also follows a rotation done by another session's worker.
        while True:
            time.sleep(LOG_CHECK_INTERVAL)
            try:
                fresh = _open_log(args.home)
                try:
                    _follow_log(fresh)
                finally:
                    os.close(fresh)
            except OSError as exc:
                _note(f"log check failed, trying again at the next one: {exc}")

    async def start(session_args, hold, **options):
        timeout = session_args.startup_timeout
        waiting_since = time.monotonic()
        first_connect_timeout = None
        if await lock.acquire(timeout if math.isfinite(timeout) and timeout > 0 else 0):
            # The wait counts against --startup-timeout, which clients size their tool timeouts by.
            first_connect_timeout = max(1.0, timeout - (time.monotonic() - waiting_since))
        try:
            await cli.run_controller(session_args, hold=hold, first_connect_timeout=first_connect_timeout, **options)
        finally:
            lock.release()

    async def serve(host):
        try:
            await cli.run_mcp(host)
        except* (BrokenPipeError, ConnectionError):
            pass  # The client left while a reply was on its way; cleanup follows.
        finally:
            # The SDK just pointed fds 0 and 1 back at the relay. Cleanup output belongs in the log.
            os.dup2(2, 1)
            null = os.open(os.devnull, os.O_RDONLY)
            os.dup2(null, 0)
            os.close(null)
            # Separately: after the relay's own half-close, macOS refuses SHUT_RDWR and leaves the write side open.
            for how in (socket.SHUT_WR, socket.SHUT_RD):
                with contextlib.suppress(OSError):
                    wire.shutdown(how)

    async def main():
        host = cli.DeferredHost(args, start=start, on_close=begin_cleanup)
        threading.Thread(target=watch_client, daemon=True).start()
        threading.Thread(target=keep_log_bounded, daemon=True).start()
        code = 0
        try:
            await cli.run_mcp_deferred(args, serve=serve, exit=finish, host=host)
        except BaseException:
            _report_failure()
            code = 1
        finish(code)  # An interpreter exit could wait on an SDK or executor thread.

    asyncio.run(main())
