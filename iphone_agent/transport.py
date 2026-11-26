"""UDID-bound USB port forwarding, with deterministic resource cleanup."""
import asyncio
import logging
import time
from pymobiledevice3.tcp_forwarder import UsbmuxTcpForwarder


class RepeatLimit(logging.Filter):
    """Pass each message once per interval, saying how many repeats were dropped.

    The forwarder logs one error per refused connection, several a second while WDA starts.
    Messages are told apart by their text, so a different failure always gets through.
    """
    def __init__(self, interval=60.0):
        super().__init__()
        self.interval = interval
        self.seen = {}

    def filter(self, record):
        now = time.monotonic()
        last, dropped = self.seen.get(record.msg, (None, 0))
        if last is not None and now - last < self.interval:
            self.seen[record.msg] = (last, dropped + 1)
            return False
        self.seen[record.msg] = (now, 0)
        if dropped:
            record.msg = f"{record.msg} ({dropped} repeats dropped)"
        return True


FORWARDER_LOG_LIMIT = RepeatLimit()
logging.getLogger("pymobiledevice3.tcp_forwarder").addFilter(FORWARDER_LOG_LIMIT)


class USBForward:
    host = "127.0.0.1"
    def __init__(self, udid, remote_port=8100, local_port=0):
        if not udid:
            raise ValueError("A device UDID is required")
        self.ready = asyncio.Event()
        self.forwarder = UsbmuxTcpForwarder(udid, remote_port, local_port,
                                          listening_event=self.ready, usbmux_connection_type="USB")
        self.task = None

    @property
    def port(self):
        return self.forwarder.listening_port

    async def open(self):
        self.task = asyncio.create_task(self.forwarder.start("127.0.0.1"))
        waiter = asyncio.create_task(self.ready.wait())
        try:
            done, _ = await asyncio.wait([self.task, waiter], timeout=10, return_when=asyncio.FIRST_COMPLETED)
            if self.task in done:
                await self.task
                raise RuntimeError("USB forwarder stopped before listening")
            if waiter not in done:
                raise TimeoutError("USB forwarding did not start")
        except BaseException:
            await self.close()
            raise
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        return self

    async def close(self):
        self.forwarder.stop()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None


class DirectConnection:
    """A caller-selected reachable device port, without a local listener."""
    def __init__(self, udid, remote_port=8100, *, host):
        if not udid or not host or not 1 <= remote_port <= 65535:
            raise ValueError("Explicit device UDID, host and port are required")
        self.udid, self.host, self.port = udid, host, remote_port

    async def open(self):
        return self

    async def close(self):
        pass
