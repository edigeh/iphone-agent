"""Give the real executable a fake phone, through PYTHONPATH, for tests/test_mcp_session.py.

By default it replaces run_controller: the session keeps the real audit journal and
a real ActivityKeepAwake whose idle-timer nudges land in events.log. FAKE_CLEANUP
selects the cleanup: "gate" waits for a release file, "fail" raises, "hang" blocks
the event loop forever. FAKE_STARTUP=hang waits forever to connect, "fail-once"
fails the first connect. FAKE_WORKER_GATE holds a cold worker until release-worker
appears in --home.

FAKE_TRANSPORT=1 keeps the real run_controller, host and keeper and replaces only
the links to the phone, so the real connect and reconnect paths run. Every
connection, WDA action and nudge is recorded in events.log. FAKE_HOLD="2:connect"
holds a numbered connection at that step. While a dead file exists every call over
the link fails, and while a locked file exists WDA says the phone is locked.

FAKE_RUNNER=1 adds the real AppleWDARunner around the fake xcodebuild in fakebin:
a usb file in --home is the cable, a wda-usb-ready file makes WDA answer over it,
and the paired network always answers. FAKE_SERVE_DELAY holds the SDK off fds 0
and 1 for that long. FAKE_DUP2_LOG names a file that records each dup2, and
FAKE_LOG_FAULT makes one log check raise OSError.

FAKE_NO_PHONE=1 keeps everything real except the USB listing, which finds no phones.
"""
import asyncio
import errno
import logging
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

if "--session-worker" in sys.argv and os.environ.get("FAKE_WORKER_CRASH"):
    os._exit(9)

if "--session-worker" in sys.argv and os.environ.get("FAKE_WORKER_GATE"):
    while not os.path.exists(os.path.join(sys.argv[sys.argv.index("--home") + 1], "release-worker")):
        time.sleep(0.01)

import aiohttp

from iphone_agent import apple_runner, cli, device, mcp_session, network

if "IPHONE_AGENT_LOCK_DIR" not in os.environ:
    raise SystemExit("tests/fake_phone_session needs IPHONE_AGENT_LOCK_DIR, so no test locks a phone in the real home")
from iphone_agent.audit import AuditLog
from iphone_agent.keepawake import ActivityKeepAwake

if os.environ.get("FAKE_CLEANUP_LIMIT"):
    mcp_session.CLEANUP_LIMIT = float(os.environ["FAKE_CLEANUP_LIMIT"])
if os.environ.get("FAKE_LOG_LIMIT"):
    mcp_session.LOG_ROTATE_BYTES = int(os.environ["FAKE_LOG_LIMIT"])
    mcp_session.LOG_CHECK_INTERVAL = 0.05
if os.environ.get("FAKE_DUP2_LOG"):
    real_dup2 = os.dup2

    def recording_dup2(fd, fd2, inheritable=True):
        with open(os.environ["FAKE_DUP2_LOG"], "a") as calls:
            calls.write(f"{os.getpid()} {fd2}\n")
        return real_dup2(fd, fd2, inheritable)

    os.dup2 = recording_dup2
if os.environ.get("FAKE_LOG_FAULT"):
    real_open_log = mcp_session._open_log
    log_fault = [OSError("the log check hit a fault")]

    def faulty_open_log(home):
        # Only the log thread opens the log off the main thread.
        if log_fault and threading.current_thread() is not threading.main_thread():
            raise log_fault.pop()
        return real_open_log(home)

    mcp_session._open_log = faulty_open_log
if os.environ.get("FAKE_SERVE_DELAY"):
    real_run_mcp = cli.run_mcp

    async def delayed_run_mcp(controller):
        await asyncio.sleep(float(os.environ["FAKE_SERVE_DELAY"]))
        await real_run_mcp(controller)

    cli.run_mcp = delayed_run_mcp


def record(home, event):
    fd = os.open(home / "events.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.write(fd, f"{os.getpid()} {event}\n".encode())
    os.close(fd)


async def released(path):
    while not path.exists():
        await asyncio.sleep(0.01)


class NudgeRecorder:
    """The WDA client the default fake session's keeper talks to."""

    def __init__(self, home):
        self.home = home

    async def locked(self, *, max_age=0.0):
        return False

    async def device_event(self, page, usage, duration):
        record(self.home, f"nudge {page:#04x} {usage:#04x}")


def write_through_rotation(fd, text):
    """On macOS a write at the instant a rotation moves its descriptor gets EBADF."""
    while True:
        try:
            os.write(fd, text.encode())
            return
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
            time.sleep(0.001)


class FakeHost:
    def __init__(self, home):
        self.home = home

    async def execute(self, action, params=None):
        record(self.home, f"execute {action}")
        if action == "slow_reply":
            await released(self.home / "release-reply")
        if action == "big_reply":
            return {"text": "x" * 200_000}
        if action == "log_lines":
            for _ in range(200):
                write_through_rotation(2, "L" * 100 + "\n")
            await asyncio.sleep(0.2)
            write_through_rotation(2, "AFTER-THE-LINES\n")
        if action == "log_marks":
            write_through_rotation(1, f"MARK-FD1-{params['mark']}\n")
            write_through_rotation(2, f"MARK-FD2-{params['mark']}\n")
        if action == "open_fds":
            return {"open": len(os.listdir("/dev/fd"))}
        if action == "wedge":
            time.sleep(3600)  # Blocks the event loop.
        if action == "break_relay":
            # Every descriptor of the relay is one socket: the reply to this call meets a broken pipe.
            for fd in range(3, 64):
                try:
                    if stat.S_ISSOCK(os.fstat(fd).st_mode):
                        socket.socket(fileno=os.dup(fd)).shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        return {"action": action, "pid": os.getpid()}


def broken_pipe_writes():
    """Everything cleanup might write with; a broken pipe here would skip the cleanup below."""
    print("STRAY stdout print")
    sys.stdout.flush()
    print("STRAY stderr print", file=sys.stderr)
    sys.stderr.flush()
    logging.getLogger("iphone_agent").warning("STRAY logging")
    os.write(1, b"STRAY fd 1\n")
    os.write(2, b"STRAY fd 2\n")
    from rich.console import Console
    Console().print("STRAY rich stdout")
    Console(stderr=True).print("STRAY rich stderr")


attempts = 0


async def run_controller(args, hold=None, first_connect_timeout=None, **options):
    global attempts
    attempts += 1
    home = args.home
    record(home, f"start timeout={args.startup_timeout:g} first={first_connect_timeout}")
    if os.environ.get("FAKE_STARTUP") == "hang":
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            record(home, "startup-cancelled")
            raise
    if os.environ.get("FAKE_STARTUP") == "fail-once" and attempts == 1:
        raise RuntimeError("the first connect failed on purpose")
    audit = AuditLog(home / "audit.jsonl")  # Raises if another controller still holds the journal.
    keeper = await ActivityKeepAwake(NudgeRecorder(home)).open()
    record(home, "connected")
    try:
        await hold(FakeHost(home))
    finally:
        record(home, f"cleanup-started sigpipe={signal.getsignal(signal.SIGPIPE)!r}")
        broken_pipe_writes()
        record(home, "cleanup-writes-ok")
        mode = os.environ.get("FAKE_CLEANUP", "gate")
        if mode == "gate":
            await released(home / "release")
        elif mode == "fail":
            audit.close()
            raise RuntimeError("cleanup failed on purpose")
        elif mode == "hang":
            child = subprocess.Popen(["/bin/sleep", "3600"], start_new_session=True)
            record(home, f"child {child.pid}")
            time.sleep(3600)
        await keeper.close()
        audit.append("keep_awake", device=args.udid, operation="stop", status=keeper.status)
        audit.append("connection_cleanup", device=args.udid, outcome="attempts_completed", errors=[])
        audit.close()
        record(home, "cleanup-finished")


class Links:
    """What FAKE_TRANSPORT=1 replaces: the USB device, the port forward and the WDA connection."""
    home = None
    connects = 0
    holds = set(filter(None, os.environ.get("FAKE_HOLD", "").split(",")))

    @classmethod
    async def hold(cls, step):
        name = f"{cls.connects}:{step}"
        if name in cls.holds:
            cls.holds.discard(name)  # Only once: closing the same connection passes through here too.
            record(cls.home, f"holding {name}")
            await asyncio.Event().wait()

    @classmethod
    def link_up(cls):
        if (cls.home / "dead").exists():
            raise aiohttp.ClientConnectionError("the link to the phone is gone")


class LinkDevice:
    def __init__(self, udid):
        self.udid = udid

    @staticmethod
    async def discover():
        return []

    async def usb_present(self):
        return not os.environ.get("FAKE_RUNNER") or (Links.home / "usb").exists()

    async def info(self):
        Links.connects += 1
        record(Links.home, f"connect {Links.connects}")
        await Links.hold("connect")
        return {}

    async def close(self):
        pass


class LinkForward:
    host, port = "127.0.0.1", 1

    def __init__(self, udid, port):
        pass

    async def open(self):
        return self

    async def close(self):
        pass


class LinkWDA:
    def __init__(self, url):
        pass

    async def open(self):
        pass

    async def close(self):
        pass

    async def call(self, method, path, body=None):
        Links.link_up()
        if os.environ.get("FAKE_RUNNER") and path == "/status" and not (Links.home / "wda-usb-ready").exists():
            raise aiohttp.ClientConnectionError("WDA is not up yet")
        return {"ready": True}

    async def locked(self, *, max_age=0.0):
        Links.link_up()
        return (Links.home / "locked").exists()

    async def device_event(self, page, usage, duration):
        Links.link_up()
        record(Links.home, f"nudge {page:#04x} {usage:#04x}")

    async def dispatch(self, action, params):
        Links.link_up()
        record(Links.home, f"dispatch {action}")
        return {"done": True}


class NetworkLinkWDA(LinkWDA):
    def __init__(self, binding):
        pass

    async def call(self, method, path, body=None):
        Links.link_up()
        return {"ready": True, "build": {"productBundleIdentifier": "com.fake.runner"}}


class LinkBinding:
    host, port = "fe80::1", 8100

    def __init__(self, udid):
        self.udid = udid

    @classmethod
    async def for_device(cls, udid, port):
        return cls(udid)

    def connection_factory(self, udid, port):
        return LinkForward(udid, port)


if os.environ.get("FAKE_NO_PHONE"):
    async def no_phones():
        return []

    device.list_devices = no_phones
elif os.environ.get("FAKE_TRANSPORT"):
    from pathlib import Path
    Links.home = Path(sys.argv[sys.argv.index("--home") + 1])
    cli.DeviceServices, cli.USBForward, cli.WDAClient = LinkDevice, LinkForward, LinkWDA
    cli.STARTUP_POLL_INTERVAL = 0.01
    if os.environ.get("FAKE_RUNNER"):
        async def nothing(self, *args):
            pass

        def launch_plan(self, plan):
            path = self.home / f"wda-launch-{os.getpid()}-{time.time_ns()}.xctestrun"
            path.write_text("plan")
            return path

        runner = apple_runner.AppleWDARunner
        runner._validate_product = lambda self: Links.home / "plan"
        runner._verify_signature = runner._prepare_device = nothing
        runner._write_launch_plan = launch_plan
        runner._validate_status = lambda self, value: value
        network.NetworkBinding, network.BoundWDAClient = LinkBinding, NetworkLinkWDA
        cli.USB_RECHECK_INTERVAL = 0.05
else:
    cli.run_controller = run_controller
