"""MCP mode answers clients without a phone and connects on the first tool call."""

import asyncio
import gc
import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from iphone_agent import cli
from iphone_agent.audit import AuditLogBusy
from iphone_agent.core import ConnectionUnavailable

HOME = Path("/nonexistent/iphone-agent-home")  # Named in error text only; nothing is written there.


class FakeHost:
    def __init__(self, events):
        self.events = events

    async def execute(self, action, params=None):
        self.events.append(("execute", action))
        return {"action": action}


def starter(events, outcomes):
    """Each run takes the next outcome: an exception to raise, or "ready" to hold a host."""

    async def start(args, hold, **options):
        events.append("start")
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "slow":
            await asyncio.sleep(3600)
        try:
            await hold(FakeHost(events))
        finally:
            events.append("cleanup")

    return start


async def test_unplugged_phone_fails_only_the_call_and_the_next_call_retries():
    events = []
    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=starter(events, [
        RuntimeError("USB device X is not connected"), "ready"]))
    with pytest.raises(ConnectionUnavailable, match="USB device X is not connected.*Connect it by USB"):
        await host.execute("state")
    assert await host.execute("state") == {"action": "state"}
    assert events == ["start", "start", ("execute", "state")]
    await host.close()
    assert events[-1] == "cleanup"


async def test_ready_connection_is_reused_and_released_on_close():
    events = []
    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=starter(events, ["ready"]))
    await host.execute("state")
    await host.execute("screenshot")
    assert events.count("start") == 1 and "cleanup" not in events
    await host.close()
    assert events[-1] == "cleanup"


async def test_abandoned_call_leaves_startup_running_for_the_next_call():
    events = []
    gate = asyncio.Event()

    async def start(args, hold, **options):
        events.append("start")
        await gate.wait()
        await hold(FakeHost(events))

    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=start)
    call = asyncio.create_task(host.execute("state"))
    await asyncio.sleep(0)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    gate.set()
    assert await host.execute("state") == {"action": "state"}
    assert events.count("start") == 1
    await host.close()


async def test_close_cancels_an_unfinished_startup():
    events = []
    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=starter(events, ["slow"]))
    call = asyncio.create_task(host.execute("state"))
    await asyncio.sleep(0.01)
    await host.close()
    with pytest.raises(ConnectionUnavailable, match="cancelled"):
        await call


async def test_cleanup_failure_after_serving_is_raised_on_close():
    async def start(args, hold, **options):
        await hold(FakeHost([]))
        raise RuntimeError("restore failed")

    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=start)
    await host.execute("state")
    with pytest.raises(RuntimeError, match="restore failed"):
        await host.close()


async def test_cancelled_startup_cleanup_errors_are_raised_on_close():
    started = asyncio.Event()

    async def start(args, hold, **options):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError as cancelled:
            raise BaseExceptionGroup("startup and cleanup", [cancelled, RuntimeError("restore failed")])

    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=start)
    call = asyncio.create_task(host.execute("state"))
    await started.wait()
    with pytest.raises(ExceptionGroup, match="startup and cleanup") as raised:
        await host.close()
    assert [str(error) for error in raised.value.exceptions] == ["restore failed"]
    with pytest.raises(ConnectionUnavailable):
        await call


def failing_start(failure):
    runs = []

    async def start(args, hold, **options):
        runs.append((args, options))
        raise failure

    start.runs = runs
    return start


async def test_call_names_the_startup_error_of_a_group_and_not_the_cleanup():
    failure = BaseExceptionGroup("Connection startup and cleanup failed", [RuntimeError("the real cause"), OSError("cleanup")])
    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=failing_start(failure))
    with pytest.raises(ConnectionUnavailable, match="RuntimeError: the real cause") as raised:
        await host.execute("state")
    assert "cleanup failed" not in str(raised.value)
    with pytest.raises(ExceptionGroup):
        await host.close()  # The cleanup error stays in the log and in the exit code.


async def test_call_names_an_error_with_no_message_by_its_type_alone():
    silent = TimeoutError()
    silent.__cause__ = asyncio.CancelledError()  # What asyncio.timeout leaves behind says nothing either.
    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=failing_start(silent))
    with pytest.raises(ConnectionUnavailable, match=r"not available \(TimeoutError\)\. "):
        await host.execute("state")
    await host.close()


async def test_call_names_the_last_error_behind_a_timeout():
    cause = ConnectionResetError()
    expired = TimeoutError("Startup did not finish")
    expired.__cause__ = cause
    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=failing_start(expired))
    with pytest.raises(ConnectionUnavailable, match=r"\(TimeoutError: Startup did not finish; last error ConnectionResetError\)"):
        await host.execute("state")
    await host.close()


async def test_busy_audit_journal_keeps_its_text_inside_a_group():
    busy = AuditLogBusy("Another controller is using the journal. Stop it, then retry the call.")
    failure = BaseExceptionGroup("Connection startup and cleanup failed",
                                 [BaseExceptionGroup("nested", [busy]), OSError("cleanup")])
    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=failing_start(failure))
    with pytest.raises(ConnectionUnavailable) as raised:
        await host.execute("state")
    assert str(raised.value) == str(busy)
    with pytest.raises(ExceptionGroup):
        await host.close()


async def test_connection_unavailable_inside_a_group_is_passed_through():
    busy = ConnectionUnavailable("Another session is using it.")
    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=failing_start(BaseExceptionGroup("failed", [busy, OSError("cleanup")])))
    with pytest.raises(ConnectionUnavailable) as raised:
        await host.execute("state")
    assert raised.value is busy
    with pytest.raises(ExceptionGroup):
        await host.close()


async def test_reported_failure_with_a_kept_cleanup_failure_raises_that_on_close():
    failure = RuntimeError("the real cause")
    failure.cleanup_failure = OSError("cleanup")
    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=failing_start(failure))
    with pytest.raises(ConnectionUnavailable, match="the real cause"):
        await host.execute("state")
    with pytest.raises(OSError, match="cleanup"):
        await host.close()


async def test_reported_failure_with_nothing_else_is_not_raised_again_on_close():
    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=failing_start(RuntimeError("the real cause")))
    with pytest.raises(ConnectionUnavailable):
        await host.execute("state")
    await host.close()


async def test_failed_startup_makes_no_further_connection_on_close():
    start = failing_start(RuntimeError("link lost"))
    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=start)
    with pytest.raises(ConnectionUnavailable, match="link lost"):
        await host.execute("state")
    await host.close()
    assert len(start.runs) == 1


async def test_a_served_session_makes_no_further_connection_on_close():
    runs = []

    async def start(args, hold, **options):
        runs.append(options)
        await hold(FakeHost([]))

    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=start)
    await host.execute("state")
    await host.close()
    assert runs == [{}]


def test_failure_seen_by_no_caller_is_retrieved_before_retrying():
    unretrieved = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: unretrieved.append(context["message"]))
        gate, runs = asyncio.Event(), []

        async def start(args, hold, **options):
            runs.append("start")
            if len(runs) == 1:
                await gate.wait()
                raise RuntimeError("not connected")
            await hold(FakeHost([]))

        host = cli.DeferredHost(SimpleNamespace(home=HOME), start=start)
        call = asyncio.create_task(host.execute("state"))
        await asyncio.sleep(0)
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        gate.set()
        await asyncio.sleep(0.01)
        assert await host.execute("state") == {"action": "state"}
        await host.close()
        gc.collect()
        await asyncio.sleep(0)

    # A private loop, so the abandoned task is collected and an unretrieved exception is logged.
    asyncio.run(scenario())
    assert unretrieved == []


async def test_calls_after_close_never_start_a_connection():
    events = []
    host = cli.DeferredHost(SimpleNamespace(home=HOME), start=starter(events, ["ready"]))
    await host.close()
    with pytest.raises(ConnectionUnavailable, match="shutting down"):
        await host.execute("state")
    assert events == []


async def test_sigterm_cleans_up_and_exits_without_waiting_for_serve(monkeypatch):
    events = []
    monkeypatch.setattr(cli, "run_controller", starter(events, ["ready"]))
    exited = asyncio.get_running_loop().create_future()

    async def serve(host):
        await host.execute("state")
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGTERM)  # A repeated signal must not interrupt cleanup.
        await asyncio.Event().wait()  # Like the SDK's uninterruptible stdin read.

    task = asyncio.create_task(cli.run_mcp_deferred(SimpleNamespace(), serve=serve, exit=exited.set_result))
    assert await asyncio.wait_for(exited, 5) == 0
    assert events[-1] == "cleanup" and events.count("cleanup") == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_real_server_without_phone_completes_handshake_and_reports_tool_error(tmp_path):
    # Regression: the server used to exit before initialize, so clients saw "Connection closed".
    # FAKE_NO_PHONE answers the USB listing with no phones, so the real path runs without asking usbmuxd.
    fake = str(Path(__file__).parent / "fake_phone_session")
    inherited = os.environ.get("PYTHONPATH")
    params = StdioServerParameters(
        command=str(Path(sys.executable).parent / "iphone-agent"),
        args=["--home", str(tmp_path), "mcp", "--udid", "00000000-NOT-A-CONNECTED-PHONE",
              "--no-keep-awake", "--startup-timeout", "5"],
        env={"FAKE_NO_PHONE": "1", "PYTHONPATH": fake + (os.pathsep + inherited if inherited else ""),
             "IPHONE_AGENT_LOCK_DIR": os.environ["IPHONE_AGENT_LOCK_DIR"]})
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await asyncio.wait_for(session.initialize(), 30)
        tools = {tool.name for tool in (await session.list_tools()).tools}
        assert {"iphone_capabilities", "iphone_state", "iphone_screenshot", "iphone_action"} <= tools
        result = await asyncio.wait_for(session.call_tool("iphone_capabilities", {}), 30)
        assert result.is_error
        assert "USB device 00000000-NOT-A-CONNECTED-PHONE is not connected" in result.content[0].text


@pytest.mark.parametrize("options, advice", [({}, "Connect it by USB"),
                                             ({"paired_network": True}, "Keep it on this Mac's network"),
                                             ({"network_fallback": True}, "Connect it by USB or keep it on this Mac's network")])
async def test_startup_deadline_names_its_last_error_and_transport_advice(options, advice):
    async def start(args, hold, **options):
        try:
            raise RuntimeError("The iPhone is locked, so Xcode cannot mount its developer disk image")
        except RuntimeError as cause:
            raise TimeoutError("Startup did not reach a ready connection within 300s") from cause

    host = cli.DeferredHost(SimpleNamespace(**options), start=start)
    with pytest.raises(ConnectionUnavailable) as raised:
        await host.execute("state")
    message = str(raised.value)
    assert "within 300s; last error RuntimeError: The iPhone is locked" in message and advice in message
    await host.close()
