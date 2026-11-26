"""ActivityKeepAwake against a fake WDA client, on short real intervals."""

import asyncio
import math
import time
from types import SimpleNamespace

import pytest

import iphone_agent.keepawake as module
from iphone_agent.keepawake import ActivityKeepAwake

INTERVAL = 0.02
WAIT = 5.0


class FakeWDA:
    """Answers lock-state reads and records idle-timer nudges; queued exceptions fail the next calls."""

    def __init__(self, locked=False):
        self.is_locked = locked
        self.lock_reads = 0
        self.nudges = []
        self.lock_failures = []
        self.nudge_failures = []
        self.nudge_hangs = False

    async def locked(self, *, max_age=0.0):
        self.lock_reads += 1
        if self.lock_failures:
            raise self.lock_failures.pop(0)
        return self.is_locked

    async def device_event(self, page, usage, duration):
        if self.nudge_hangs:
            await asyncio.Event().wait()
        if self.nudge_failures:
            raise self.nudge_failures.pop(0)
        self.nudges.append((page, usage, duration))


@pytest.fixture
def clock(monkeypatch):
    """The keeper's clock: real time plus an offset the test can move forward."""
    offset = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0]))
    return offset


async def wait_until(predicate, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(INTERVAL / 4)


async def intervals(count):
    await asyncio.sleep(INTERVAL * count)


@pytest.fixture
async def keepers():
    opened = []
    yield opened
    for keeper in opened:
        await keeper.close()


async def started(keepers, wda, **options):
    keeper = await ActivityKeepAwake(wda, interval=options.pop("interval", INTERVAL), **options).open()
    keepers.append(keeper)
    return keeper


async def test_nudges_arrive_on_the_interval_while_active(keepers):
    wda = FakeWDA()
    began = time.monotonic()
    keeper = await started(keepers, wda, idle_limit=60)
    assert wda.nudges == [(0x07, 0x68, 0.02)]  # The first one is sent before open returns.
    await wait_until(lambda: len(wda.nudges) >= 5)
    elapsed = time.monotonic() - began
    assert len(wda.nudges) <= elapsed / INTERVAL + 2  # No more than one per interval.
    assert set(wda.nudges) == {(0x07, 0x68, 0.02)}
    status = keeper.status
    assert status["active"] is True and status["locked"] is False and status["lastError"] is None
    assert status["secondsSinceNudge"] is not None and status["secondsSinceNudge"] <= 1.0


async def test_status_describes_the_method_and_its_limits(keepers):
    keeper = ActivityKeepAwake(FakeWDA(), idle_limit=90, interval=INTERVAL)
    status = keeper.status
    assert status["active"] is False and status["secondsSinceNudge"] is None and status["locked"] is None
    await keeper.open()
    keepers.append(keeper)
    status = keeper.status
    assert status["method"] == "input-nudge" and status["changesSettings"] is False
    assert status["intervalSeconds"] == INTERVAL and status["idleLimitSeconds"] == 90.0
    assert 0 <= status["idleSeconds"] < 1


async def test_nudges_stop_once_the_agent_is_idle_and_resume_after_activity(keepers, clock):
    wda = FakeWDA()
    keeper = await started(keepers, wda, idle_limit=10)
    await wait_until(lambda: len(wda.nudges) >= 2)
    clock[0] += 11  # The last agent call is now older than the idle limit.
    await intervals(2)  # A pass that was already in flight may still finish.
    sent, reads = len(wda.nudges), wda.lock_reads
    await intervals(6)
    assert len(wda.nudges) == sent and wda.lock_reads == reads  # Not even the lock state is read.
    assert keeper.status["active"] is False and keeper.status["idleSeconds"] >= 10
    keeper.activity()
    await wait_until(lambda: len(wda.nudges) >= sent + 2)
    assert keeper.status["active"] is True and keeper.status["idleSeconds"] < 10


async def test_a_locked_phone_gets_no_nudge_and_the_status_says_so(keepers):
    wda = FakeWDA(locked=True)
    keeper = await started(keepers, wda, idle_limit=60)
    await wait_until(lambda: wda.lock_reads >= 4)
    assert wda.nudges == []
    status = keeper.status
    assert status["locked"] is True and status["active"] is False and status["lastError"] is None
    wda.is_locked = False
    await wait_until(lambda: len(wda.nudges) >= 2)
    assert keeper.status["locked"] is False and keeper.status["active"] is True
    wda.is_locked = True
    await wait_until(lambda: keeper.status["locked"] is True)
    sent = len(wda.nudges)
    await intervals(4)
    assert len(wda.nudges) == sent


@pytest.mark.parametrize("step", ["lock read", "nudge"])
async def test_an_error_lands_in_the_status_the_loop_goes_on_and_a_success_clears_it(keepers, step):
    wda = FakeWDA()
    failures = wda.lock_failures if step == "lock read" else wda.nudge_failures
    keeper = await started(keepers, wda, idle_limit=60)
    failures.extend([ConnectionResetError("the link dropped")] * 3)
    await wait_until(lambda: keeper.status["lastError"] is not None)
    assert keeper.status["lastError"] == "ConnectionResetError: the link dropped"
    assert keeper._task is not None and not keeper._task.done()
    await wait_until(lambda: not failures and keeper.status["lastError"] is None)
    sent = len(wda.nudges)
    await wait_until(lambda: len(wda.nudges) > sent)
    assert keeper.status["active"] is True


async def test_an_error_without_a_message_is_named_by_its_type(keepers):
    wda = FakeWDA()
    wda.nudge_failures.append(RuntimeError())
    keeper = await started(keepers, wda, idle_limit=60)
    assert keeper.status["lastError"] == "RuntimeError"


async def test_a_nudge_that_hangs_is_cut_off_at_the_interval_and_reported(keepers):
    wda = FakeWDA()
    wda.nudge_hangs = True
    keeper = await started(keepers, wda, idle_limit=60)  # open() itself returns after one interval.
    assert keeper.status["lastError"] == "TimeoutError"
    wda.nudge_hangs = False
    await wait_until(lambda: wda.nudges and keeper.status["lastError"] is None)


async def test_open_does_not_raise_when_the_first_nudge_fails(keepers):
    wda = FakeWDA()
    wda.nudge_failures.append(OSError("WDA is not answering"))
    keeper = await started(keepers, wda, idle_limit=60)
    assert keeper.status["lastError"] == "OSError: WDA is not answering"
    await wait_until(lambda: len(wda.nudges) >= 1)  # The loop runs anyway, and the next try succeeds.
    assert keeper.status["lastError"] is None


async def test_open_again_while_running_starts_no_second_loop(keepers):
    wda = FakeWDA()
    keeper = await started(keepers, wda, idle_limit=60)
    task = keeper._task
    assert await keeper.open() is keeper
    assert keeper._task is task and len(wda.nudges) == 1


async def test_close_stops_the_loop(keepers):
    wda = FakeWDA()
    keeper = await started(keepers, wda, idle_limit=60)
    await wait_until(lambda: len(wda.nudges) >= 2)
    task = keeper._task
    await keeper.close()
    assert task.done()
    sent, reads = len(wda.nudges), wda.lock_reads
    await intervals(5)
    assert len(wda.nudges) == sent and wda.lock_reads == reads
    assert keeper.status["active"] is False
    await keeper.close()  # A second close is harmless.


async def test_close_before_open_is_harmless():
    await ActivityKeepAwake(FakeWDA()).close()


@pytest.mark.parametrize("name", ["idle_limit", "interval"])
@pytest.mark.parametrize("value", [0, -1, math.inf, math.nan, True, "10"])
def test_invalid_limits_are_rejected(name, value):
    with pytest.raises(ValueError, match=name):
        ActivityKeepAwake(FakeWDA(), **{name: value})


def test_an_interval_is_required():
    with pytest.raises(ValueError, match="interval"):
        ActivityKeepAwake(FakeWDA(), interval=None)


async def test_no_idle_limit_keeps_nudging_however_long_the_agent_is_idle(keepers, clock):
    wda = FakeWDA()
    keeper = await started(keepers, wda, idle_limit=None)
    clock[0] += 10 * 24 * 3600
    sent = len(wda.nudges)
    await wait_until(lambda: len(wda.nudges) >= sent + 2)
    assert keeper.status["active"] is True and keeper.status["idleLimitSeconds"] is None


async def test_work_in_progress_keeps_nudging_past_the_idle_limit(keepers, clock):
    wda = FakeWDA()
    recording = [True]
    keeper = await started(keepers, wda, idle_limit=10, busy=lambda: recording[0])
    clock[0] += 11
    sent = len(wda.nudges)
    await wait_until(lambda: len(wda.nudges) >= sent + 2)
    assert keeper.status["active"] is True
    recording[0] = False
    await intervals(2)
    sent = len(wda.nudges)
    await intervals(6)
    assert len(wda.nudges) == sent and keeper.status["active"] is False


def test_defaults_are_ten_second_nudges_until_thirty_idle_minutes():
    keeper = ActivityKeepAwake(FakeWDA())
    assert (keeper.interval, keeper.idle_limit) == (10.0, 1800.0)
