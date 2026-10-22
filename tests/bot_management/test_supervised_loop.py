import asyncio
import logging

import pytest

from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest
from grid_trading_bot.core.bot_management.supervised_loop import LoopState, SupervisedLoop

INTERVAL = 0.01


class Recorder:
    def __init__(self):
        self.sent: list[tuple[NotificationType, dict]] = []
        self.stops: list[StopRequest] = []

    async def notify(self, notification_type, **details):
        self.sent.append((notification_type, details))

    async def on_stop(self, request):
        self.stops.append(request)


@pytest.fixture
def recorder():
    return Recorder()


@pytest.fixture
def event_bus(recorder):
    bus = EventBus()
    bus.subscribe(Events.STOP_BOT, recorder.on_stop)
    return bus


def _body(outcomes):
    """A body that raises the n-th outcome at its n-th call when it is an exception, and returns otherwise."""
    calls: list[int] = []

    async def body():
        calls.append(len(calls))
        if len(calls) <= len(outcomes) and isinstance(outcomes[len(calls) - 1], BaseException):
            raise outcomes[len(calls) - 1]

    return body, calls


def _loop(body, event_bus, recorder, run_first=True):
    return SupervisedLoop("Test loop", INTERVAL, body, run_first=run_first, event_bus=event_bus, notify=recorder.notify)


async def _until(condition, timeout=1.0):
    async def wait():
        while not condition():
            await asyncio.sleep(INTERVAL / 2)

    await asyncio.wait_for(wait(), timeout)


async def _cancel(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_one_failure_alerts_once_and_the_loop_keeps_running(event_bus, recorder):
    body, calls = _body([RuntimeError("boom")])
    loop = _loop(body, event_bus, recorder)
    task = asyncio.create_task(loop.run())

    await _until(lambda: len(calls) >= 3)
    assert loop.state.running
    await _cancel(task)

    errors = [details for kind, details in recorder.sent if kind is NotificationType.ERROR_OCCURRED]
    assert len(errors) == 1
    assert errors[0]["error_details"].startswith("Test loop failed: RuntimeError: boom.")
    assert recorder.stops == []


async def test_a_success_after_failures_sends_the_recovered_alert_and_resets_the_streak(event_bus, recorder):
    body, calls = _body([RuntimeError("a"), RuntimeError("b")])
    loop = _loop(body, event_bus, recorder)
    task = asyncio.create_task(loop.run())

    await _until(lambda: len(calls) >= 3)
    await _cancel(task)

    assert [kind for kind, _ in recorder.sent] == [NotificationType.ERROR_OCCURRED, NotificationType.LOOP_RECOVERED]
    assert recorder.sent[1][1] == {"loop": "Test loop", "failures": 2}
    assert loop.state == LoopState(running=False, consecutive_failures=0, last_error=None, cancelled=True)


async def test_three_failures_in_a_row_request_one_crash_stop_and_end_the_loop(event_bus, recorder):
    body, calls = _body([RuntimeError("1"), RuntimeError("2"), RuntimeError("3"), RuntimeError("4")])
    loop = _loop(body, event_bus, recorder)

    await asyncio.wait_for(loop.run(), timeout=1)
    await _until(lambda: recorder.stops)

    assert len(calls) == 3
    assert recorder.stops == [StopRequest(StopKind.CRASH, "Test loop failed 3 times in a row: RuntimeError: 3")]
    assert loop.state == LoopState(running=False, consecutive_failures=3, last_error="RuntimeError: 3")
    assert [kind for kind, _ in recorder.sent] == [NotificationType.ERROR_OCCURRED]


async def test_a_cancelled_loop_ends_without_an_alert(event_bus, recorder):
    body, calls = _body([])
    loop = _loop(body, event_bus, recorder, run_first=False)
    task = asyncio.create_task(loop.run())

    await _until(lambda: calls)
    await _cancel(task)

    assert recorder.sent == []
    assert recorder.stops == []
    assert loop.state == LoopState(running=False, consecutive_failures=0, last_error=None, cancelled=True)


async def test_run_first_false_waits_one_interval_before_the_first_pass(event_bus, recorder):
    body, calls = _body([])
    loop = SupervisedLoop("Test loop", 10.0, body, run_first=False, event_bus=event_bus, notify=recorder.notify)
    task = asyncio.create_task(loop.run())

    await asyncio.sleep(0.05)
    assert calls == []
    await _cancel(task)


async def test_a_cancelled_child_counts_as_a_failure_not_as_the_loop_cancellation(event_bus, recorder):
    async def body():
        child = asyncio.create_task(asyncio.sleep(10))
        child.cancel()
        await child

    loop = _loop(body, event_bus, recorder)

    await asyncio.wait_for(loop.run(), timeout=1)
    await _until(lambda: recorder.stops)

    assert [request.kind for request in recorder.stops] == [StopKind.CRASH]
    assert loop.state.consecutive_failures == 3


async def test_the_first_failure_alert_names_the_error_type_when_the_message_is_empty(event_bus, recorder):
    body, calls = _body([RuntimeError()])
    loop = _loop(body, event_bus, recorder)
    task = asyncio.create_task(loop.run())

    await _until(lambda: len(calls) >= 2)
    await _cancel(task)

    [alert] = [d["error_details"] for kind, d in recorder.sent if kind is NotificationType.ERROR_OCCURRED]
    assert alert.startswith("Test loop failed: RuntimeError")


async def test_the_first_failure_alert_names_a_cancelled_child(event_bus, recorder):
    async def body():
        child = asyncio.create_task(asyncio.sleep(10))
        child.cancel()
        await child

    loop = _loop(body, event_bus, recorder)

    await asyncio.wait_for(loop.run(), timeout=1)

    [alert] = [d["error_details"] for kind, d in recorder.sent if kind is NotificationType.ERROR_OCCURRED]
    assert alert.startswith("Test loop failed: CancelledError")


async def test_without_an_event_bus_the_third_failure_logs_and_ends_the_loop(recorder, caplog):
    body, _ = _body([RuntimeError("1"), RuntimeError("2"), RuntimeError("3")])
    loop = SupervisedLoop("Test loop", INTERVAL, body, run_first=True, event_bus=None, notify=None)

    with caplog.at_level(logging.ERROR, logger="SupervisedLoop"):
        await asyncio.wait_for(loop.run(), timeout=1)

    assert not loop.state.running
    assert any("no event bus" in record.getMessage() for record in caplog.records)


async def test_a_failing_notification_does_not_end_the_loop(event_bus, recorder):
    async def broken_notify(*_args, **_kwargs):
        raise OSError("apprise down")

    body, calls = _body([RuntimeError("boom")])
    loop = SupervisedLoop("Test loop", INTERVAL, body, run_first=True, event_bus=event_bus, notify=broken_notify)
    task = asyncio.create_task(loop.run())

    await _until(lambda: len(calls) >= 3)
    assert loop.state.running
    await _cancel(task)
