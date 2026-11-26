"""An MCP session that stops being used gives the phone back; its next call connects again."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from iphone_agent import cli


class Host:
    def __init__(self, events, delay=0.0):
        self.events, self.delay = events, delay

    async def execute(self, action, params=None):
        await asyncio.sleep(self.delay)
        self.events.append(("execute", action))
        return action


def starter(events, delay=0.0, cleanup_error=None):
    async def start(args, hold, **options):
        events.append("start")
        try:
            await hold(Host(events, delay))
        finally:
            events.append("cleanup")
            if cleanup_error is not None:
                raise cleanup_error

    return start


def arguments(idle_timeout):
    return SimpleNamespace(idle_timeout=idle_timeout, home=Path("/nonexistent"))


async def until(predicate, limit=5.0):
    async with asyncio.timeout(limit):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_session_disconnects_after_the_idle_timeout_and_the_next_call_reconnects():
    events = []
    host = cli.DeferredHost(arguments(0.2), start=starter(events))
    await host.execute("state")
    await until(lambda: "cleanup" in events)
    assert events == ["start", ("execute", "state"), "cleanup"]
    assert await host.execute("screenshot") == "screenshot"
    assert events == ["start", ("execute", "state"), "cleanup", "start", ("execute", "screenshot")]
    await host.close()
    assert events.count("cleanup") == 2


async def test_calls_keep_the_session_and_only_a_gap_longer_than_the_timeout_ends_it():
    events = []
    host = cli.DeferredHost(arguments(1.5), start=starter(events))
    for _ in range(5):
        await host.execute("state")
        await asyncio.sleep(0.5)
    assert events.count("start") == 1 and "cleanup" not in events
    await host.close()


async def test_call_still_running_past_the_idle_timeout_is_not_cut_off():
    events = []
    host = cli.DeferredHost(arguments(0.2), start=starter(events, delay=1.6))
    assert await host.execute("recording_stop") == "recording_stop"
    assert events == ["start", ("execute", "recording_stop")]
    await until(lambda: "cleanup" in events)
    await host.close()


@pytest.mark.parametrize("idle_timeout", [None, 0, -5, float("inf"), float("nan"), "30"])
async def test_no_usable_idle_timeout_keeps_the_session_until_close(idle_timeout):
    events = []
    host = cli.DeferredHost(arguments(idle_timeout), start=starter(events))
    await host.execute("state")
    assert host._idle_task is None
    await host.close()
    assert events == ["start", ("execute", "state"), "cleanup"]


async def test_close_ends_the_idle_watch_and_a_failed_idle_cleanup_does_not_stop_the_next_connect(caplog):
    events = []
    host = cli.DeferredHost(arguments(0.2), start=starter(events, cleanup_error=RuntimeError("runner would not stop")))
    await host.execute("state")
    await until(lambda: "cleanup" in events)
    await until(lambda: "runner would not stop" in caplog.text)
    assert await host.execute("state") == "state"
    watch = host._idle_task
    with pytest.raises(RuntimeError, match="runner would not stop"):
        await host.close()
    assert watch.done()


async def test_recording_left_running_keeps_the_session_past_the_idle_timeout():
    events = []
    media = SimpleNamespace(busy=True)

    class RecordingHost(Host):
        pass

    async def start(args, hold, **options):
        events.append("start")
        host = RecordingHost(events)
        host.media = media
        try:
            await hold(host)
        finally:
            events.append("cleanup")

    cli_host = cli.DeferredHost(arguments(0.2), start=start)
    await cli_host.execute("recording_start")
    await asyncio.sleep(1.0)
    assert "cleanup" not in events
    media.busy = False  # The recording was stopped by something other than a call, or finished.
    await until(lambda: "cleanup" in events, limit=cli.IDLE_RECHECK_INTERVAL + 3)
    await cli_host.close()


async def test_client_leaving_during_an_idle_disconnect_lets_its_cleanup_finish():
    events = []

    async def start(args, hold, **options):
        events.append("start")
        try:
            await hold(Host(events))
        finally:
            events.append("cleanup-started")
            await asyncio.sleep(0.5)  # Stopping the runner takes a moment; a cancel here would skip the rest.
            events.append("cleanup-finished")

    host = cli.DeferredHost(arguments(0.2), start=start)
    await host.execute("state")
    await until(lambda: "cleanup-started" in events)
    await host.close()
    assert events == ["start", ("execute", "state"), "cleanup-started", "cleanup-finished"]


async def test_idle_watch_sleeps_until_the_next_connection_instead_of_polling(monkeypatch):
    events, sleeps = [], []
    real_sleep = asyncio.sleep

    async def sleep(delay, *args):
        sleeps.append(delay)
        await real_sleep(delay, *args)

    host = cli.DeferredHost(arguments(0.2), start=starter(events))
    await host.execute("state")
    await until(lambda: "cleanup" in events)
    await real_sleep(0.05)
    monkeypatch.setattr(cli.asyncio, "sleep", sleep)
    await real_sleep(1.2)
    assert sleeps == [] and not host._idle_task.done()
    monkeypatch.undo()
    await host.close()


async def test_disconnect_closes_the_connection_at_once_and_the_next_call_reconnects():
    events = []
    host = cli.DeferredHost(arguments(1800.0), start=starter(events))
    await host.execute("state")
    assert await host.disconnect() == {"disconnected": True,
                                       "detail": "The iPhone connection is closed; the next tool call connects again."}
    assert events == ["start", ("execute", "state"), "cleanup"]
    assert await host.execute("screenshot") == "screenshot"
    assert events[-2:] == ["start", ("execute", "screenshot")]
    await host.close()
    assert events.count("cleanup") == 2


async def test_disconnect_without_a_connection_starts_nothing():
    events = []
    host = cli.DeferredHost(arguments(None), start=starter(events))
    result = await host.disconnect()
    assert result["disconnected"] is False and events == []
    await host.execute("state")
    await host.disconnect()
    assert (await host.disconnect())["disconnected"] is False
    await host.close()
    assert events == ["start", ("execute", "state"), "cleanup"]


async def test_disconnect_refuses_while_a_recording_runs_and_keeps_the_session():
    events = []
    media = SimpleNamespace(busy=True)

    async def start(args, hold, **options):
        events.append("start")
        phone = Host(events)
        phone.media = media
        try:
            await hold(phone)
        finally:
            events.append("cleanup")

    host = cli.DeferredHost(arguments(None), start=start)
    await host.execute("recording_start")
    with pytest.raises(cli.ConnectionUnavailable, match="recording_stop"):
        await host.disconnect()
    assert "cleanup" not in events
    media.busy = False
    assert (await host.disconnect())["disconnected"] is True
    assert events[-1] == "cleanup"
    await host.close()


async def test_disconnect_reports_a_failed_cleanup_once_and_close_does_not_repeat_it():
    events = []
    host = cli.DeferredHost(arguments(None), start=starter(events, cleanup_error=RuntimeError("runner would not stop")))
    await host.execute("state")
    with pytest.raises(cli.ConnectionUnavailable, match="runner would not stop"):
        await host.disconnect()
    await host.close()


async def test_mcp_tool_disconnects_the_deferred_host():
    events = []
    host = cli.DeferredHost(arguments(None), start=starter(events))
    server = cli.create_mcp(host)
    await server.call_tool("iphone_state", {})
    await server.call_tool("iphone_disconnect", {})
    assert events == ["start", ("execute", "state"), "cleanup"]
    await host.close()


async def test_disconnect_refuses_while_another_call_runs_and_leaves_that_call_alone():
    events = []
    host = cli.DeferredHost(arguments(None), start=starter(events, delay=0.5))
    running = asyncio.create_task(host.execute("type_text"))
    await until(lambda: "start" in events and host._calls)
    with pytest.raises(cli.ConnectionUnavailable, match="still running"):
        await host.disconnect()
    assert await running == "type_text" and "cleanup" not in events
    assert (await host.disconnect())["disconnected"] is True
    await host.close()


async def test_a_call_after_a_cancelled_disconnect_waits_for_the_cleanup_and_reconnects():
    events = []

    class ClosingHost(Host):
        closed = False

        async def execute(self, action, params=None):
            if self.closed:
                raise RuntimeError("session closed")
            return await super().execute(action, params)

    async def start(args, hold, **options):
        events.append("start")
        phone = ClosingHost(events)
        try:
            await hold(phone)
        finally:
            phone.closed = True
            await asyncio.sleep(0.5)  # A slow runner stop.
            events.append("cleanup")

    host = cli.DeferredHost(arguments(None), start=start)
    await host.execute("state")
    disconnecting = asyncio.create_task(host.disconnect())
    await asyncio.sleep(0.1)
    disconnecting.cancel()
    await asyncio.gather(disconnecting, return_exceptions=True)
    assert await host.execute("state") == "state"
    assert events == ["start", ("execute", "state"), "cleanup", "start", ("execute", "state")]
    await host.close()


async def test_a_call_waiting_on_a_closing_connection_does_not_reconnect_once_shutdown_begins():
    events = []

    async def start(args, hold, **options):
        events.append("start")
        try:
            await hold(Host(events))
        finally:
            await asyncio.sleep(0.5)
            events.append("cleanup")

    host = cli.DeferredHost(arguments(None), start=start)
    await host.execute("state")
    disconnecting = asyncio.create_task(host.disconnect())
    await asyncio.sleep(0.1)
    disconnecting.cancel()
    await asyncio.gather(disconnecting, return_exceptions=True)
    waiting = asyncio.create_task(host.execute("state"))
    await asyncio.sleep(0.1)
    closing = host.close()
    with pytest.raises(cli.ConnectionUnavailable, match="shutting down"):
        await waiting
    await closing
    assert events.count("start") == 1
