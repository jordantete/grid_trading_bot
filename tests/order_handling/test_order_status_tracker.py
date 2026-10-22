import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock, Mock

import pytest
from tests.fakes.trading_stack import ARMING_PRICE, build_stack, reserved_matches_open_orders

from grid_trading_bot.core.grid_management.grid_level import GridCycleState
from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus
from grid_trading_bot.core.order_handling.order_status_tracker import OrderStatusTracker

TRACKER_LOGGER = "OrderStatusTracker"


@pytest.fixture
async def armed():
    """A live stack with its initial grid placed: buys on 80, 90 and 100, a sell on 120, and a tracker polling it."""
    stack = build_stack()
    await stack.manager.initialize_grid_orders(ARMING_PRICE)
    tracker = OrderStatusTracker(stack.book, stack.execution, stack.manager, polling_interval=1.0)
    return stack, tracker


def _balances(stack):
    b = stack.balances
    return (b.balance, b.crypto_balance, b.reserved_fiat, b.reserved_crypto)


def _messages(caplog, level):
    return [r.getMessage() for r in caplog.records if r.name == TRACKER_LOGGER and r.levelno == level]


class TestStatusHandling:
    async def test_a_filled_order_is_booked_and_paired(self, armed, caplog):
        stack, tracker = armed
        caplog.set_level(logging.INFO, logger=TRACKER_LOGGER)
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        reserved_fiat = stack.balances.reserved_fiat
        stack.execution.fill(buy.identifier)

        await tracker._process_open_orders()

        assert stack.book.get_order(buy.identifier).is_filled()
        sell = stack.execution.order_at(110.0, OrderSide.SELL)
        assert sell.amount == pytest.approx(buy.amount * 0.999)
        assert stack.book.get_order(sell.identifier).is_open()
        assert stack.balances.reserved_fiat == pytest.approx(reserved_fiat - buy.amount * 100.0)
        assert stack.grid.grid_levels[100.0].state == GridCycleState.READY_TO_BUY
        assert reserved_matches_open_orders(stack)
        assert f"Order {buy.identifier} filled." in _messages(caplog, logging.INFO)

    async def test_an_order_the_exchange_cannot_read_is_logged_and_left_open(self, armed, caplog):
        stack, tracker = armed
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        del stack.execution.orders[buy.identifier]
        before = _balances(stack)

        await tracker._process_open_orders()

        assert stack.book.get_order(buy.identifier).is_open()
        assert _balances(stack) == before
        assert _messages(caplog, logging.ERROR) == [
            f"Failed to query remote order with identifier {buy.identifier}: "
            f"Order {buy.identifier} not found on BTC/USDT"
        ]

    @pytest.mark.parametrize("status", [OrderStatus.CANCELED, OrderStatus.EXPIRED])
    async def test_a_cancelled_or_expired_order_is_placed_again(self, armed, caplog, status):
        stack, tracker = armed
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        before = _balances(stack)
        stack.execution.end(buy.identifier, status)

        await tracker._process_open_orders()

        assert stack.book.get_order(buy.identifier).status == status
        again = stack.execution.order_at(100.0, OrderSide.BUY)
        assert again.identifier != buy.identifier
        assert again.amount == pytest.approx(buy.amount)
        assert _balances(stack) == before
        assert stack.grid.grid_levels[100.0].state == GridCycleState.WAITING_FOR_BUY_FILL
        assert reserved_matches_open_orders(stack)
        assert f"Order {buy.identifier} ended as {status.value}." in _messages(caplog, logging.WARNING)

    async def test_a_rejected_order_frees_its_level_and_its_funds(self, armed, caplog):
        stack, tracker = armed
        buy = stack.execution.order_at(90.0, OrderSide.BUY)
        fiat, reserved_fiat = stack.balances.balance, stack.balances.reserved_fiat
        stack.execution.end(buy.identifier, OrderStatus.REJECTED)

        await tracker._process_open_orders()

        assert stack.book.get_order(buy.identifier).status == OrderStatus.REJECTED
        assert [o.price for o in stack.execution.open_orders(OrderSide.BUY)] == [80.0, 100.0]
        assert stack.balances.reserved_fiat == pytest.approx(reserved_fiat - buy.amount * 90.0)
        assert stack.balances.balance == pytest.approx(fiat + buy.amount * 90.0)
        assert stack.grid.grid_levels[90.0].state == GridCycleState.READY_TO_BUY
        assert reserved_matches_open_orders(stack)
        assert f"Order {buy.identifier} ended as rejected." in _messages(caplog, logging.WARNING)

    async def test_an_order_without_a_status_is_logged_and_left_open(self, armed, caplog):
        stack, tracker = armed
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        stack.execution.orders[buy.identifier].status = OrderStatus.UNKNOWN
        before = _balances(stack)

        await tracker._process_open_orders()

        assert stack.book.get_order(buy.identifier).is_open()
        assert _balances(stack) == before
        errors = _messages(caplog, logging.ERROR)
        assert len(errors) == 2
        assert errors[0].startswith(f"Missing 'status' in remote order object: Order(id={buy.identifier},")
        assert errors[1] == (
            "Error handling order status change: Order data from the exchange is missing the 'status' field."
        )

    async def test_an_open_order_without_fills_changes_nothing(self, armed, caplog):
        stack, tracker = armed
        caplog.set_level(logging.INFO, logger=TRACKER_LOGGER)
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        before = _balances(stack)

        await tracker._process_open_orders()

        assert stack.book.get_order(buy.identifier).is_open()
        assert _balances(stack) == before
        assert any(
            m.startswith(f"Order Order(id={buy.identifier},") and m.endswith("is still open. No fills yet.")
            for m in _messages(caplog, logging.INFO)
        )

    async def test_a_partial_fill_changes_nothing_until_the_order_ends(self, armed, caplog):
        stack, tracker = armed
        caplog.set_level(logging.INFO, logger=TRACKER_LOGGER)
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        before = _balances(stack)
        stack.execution.fill(buy.identifier, buy.amount * 0.3)

        await tracker._process_open_orders()

        local = stack.book.get_order(buy.identifier)
        assert local.is_open()
        assert local.filled == 0
        assert _balances(stack) == before
        assert stack.execution.open_orders(OrderSide.SELL) == [stack.execution.order_at(120.0, OrderSide.SELL)]
        assert stack.grid.grid_levels[100.0].state == GridCycleState.WAITING_FOR_BUY_FILL
        assert any("partially filled. Filled: " in m for m in _messages(caplog, logging.INFO))

    async def test_an_unhandled_status_is_logged_and_left_open(self, armed, caplog):
        stack, tracker = armed
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        stack.execution.orders[buy.identifier].status = "unexpected_status"
        before = _balances(stack)

        await tracker._process_open_orders()

        assert stack.book.get_order(buy.identifier).is_open()
        assert _balances(stack) == before
        assert _messages(caplog, logging.WARNING) == [
            f"Unhandled order status 'unexpected_status' for order {buy.identifier}."
        ]

    async def test_one_poll_applies_every_order_that_ended(self, armed):
        stack, tracker = armed
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        sell = stack.execution.order_at(120.0, OrderSide.SELL)
        stack.execution.fill(buy.identifier)
        stack.execution.end(sell.identifier, OrderStatus.CANCELED)

        await tracker._process_open_orders()

        assert stack.book.get_order(buy.identifier).is_filled()
        assert stack.book.get_order(sell.identifier).is_canceled()
        assert stack.execution.order_at(110.0, OrderSide.SELL).amount == pytest.approx(buy.amount * 0.999)
        assert stack.execution.order_at(120.0, OrderSide.SELL).identifier != sell.identifier
        assert reserved_matches_open_orders(stack)


class TestTrackingLifecycle:
    async def test_start_tracking_creates_monitoring_task(self, setup_tracker):
        tracker, _, _, _ = setup_tracker

        tracker.start_tracking()
        assert tracker._monitoring_task is not None
        assert not tracker._monitoring_task.done()

        await tracker.stop_tracking()

    async def test_start_tracking_warns_if_already_running(self, setup_tracker, caplog):
        tracker, _, _, _ = setup_tracker
        tracker.start_tracking()
        first = tracker._monitoring_task

        tracker.start_tracking()

        assert tracker._monitoring_task is first
        assert _messages(caplog, logging.WARNING) == ["OrderStatusTracker is already running."]
        await tracker.stop_tracking()

    async def test_stop_tracking_cancels_monitoring_task(self, setup_tracker):
        tracker, _, _, _ = setup_tracker
        tracker.start_tracking()
        task = tracker._monitoring_task

        await tracker.stop_tracking()

        assert task.done()
        assert tracker._monitoring_task is None

    async def test_track_open_order_statuses_handles_cancellation(self, setup_tracker):
        tracker, _, _, _ = setup_tracker
        tracker.start_tracking()
        await asyncio.sleep(0.1)

        await tracker.stop_tracking()

        assert tracker._monitoring_task is None
        assert tracker._active_tasks == set()

    async def test_track_open_order_statuses_handles_unexpected_error(self, setup_tracker, caplog):
        tracker, order_book, _, _ = setup_tracker
        order_book.get_open_orders.side_effect = Exception("Unexpected error")

        monitoring_task = asyncio.create_task(tracker._track_open_order_statuses())
        await asyncio.wait_for(monitoring_task, timeout=1)

        assert monitoring_task.done()
        assert not monitoring_task.cancelled()
        assert _messages(caplog, logging.ERROR) == ["Unexpected error in OrderStatusTracker: Unexpected error"]

    async def test_cancel_active_tasks(self, setup_tracker):
        tracker, _, _, _ = setup_tracker

        async def dummy_task():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(10)

        task1 = tracker._create_task(dummy_task())
        task2 = tracker._create_task(dummy_task())

        assert len(tracker._active_tasks) == 2

        await tracker._cancel_active_tasks()

        assert len(tracker._active_tasks) == 0
        assert task1.cancelled()
        assert task2.cancelled()

    async def test_create_task_adds_to_active_tasks(self, setup_tracker):
        tracker, _, _, _ = setup_tracker

        async def dummy_coro():
            await asyncio.sleep(0.1)

        task = tracker._create_task(dummy_coro())

        assert task in tracker._active_tasks
        await task
        assert task not in tracker._active_tasks


class TestStopTrackingDrain:
    @staticmethod
    def _one_query_in_flight(tracker, order_book, order_execution_strategy, query):
        order_book.get_open_orders.return_value = [Mock(identifier="order_1", symbol="BTC/USDT")]
        started = asyncio.Event()

        async def query_side_effect(*_args):
            started.set()
            return await query()

        order_execution_strategy.get_order = AsyncMock(side_effect=query_side_effect)
        return started

    async def test_stop_tracking_lets_a_status_handler_in_flight_finish(self, setup_tracker):
        tracker, order_book, order_execution_strategy, _ = setup_tracker
        handled = []

        async def slow_query():
            await asyncio.sleep(0.05)
            return Mock(identifier="order_1")

        started = self._one_query_in_flight(tracker, order_book, order_execution_strategy, slow_query)
        tracker._handle_order_status_change = AsyncMock(side_effect=lambda order: handled.append(order.identifier))
        tracker.start_tracking()
        await asyncio.wait_for(started.wait(), timeout=1)

        await asyncio.wait_for(tracker.stop_tracking(), timeout=1)

        assert handled == ["order_1"]
        assert tracker._monitoring_task is None

    async def test_stop_tracking_cancels_a_status_handler_that_outlasts_the_bound(self, setup_tracker, caplog):
        tracker, order_book, order_execution_strategy, _ = setup_tracker
        tracker.HANDLER_DRAIN_TIMEOUT = 0.05

        async def stuck_query():
            await asyncio.Event().wait()

        started = self._one_query_in_flight(tracker, order_book, order_execution_strategy, stuck_query)
        tracker.start_tracking()
        await asyncio.wait_for(started.wait(), timeout=1)
        handler = next(iter(tracker._active_tasks))

        with caplog.at_level(logging.WARNING, logger="OrderStatusTracker"):
            await asyncio.wait_for(tracker.stop_tracking(), timeout=1)

        assert handler.cancelled()
        warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "cancel" in warnings[0].getMessage().lower()


class TestPollOrder:
    @staticmethod
    def _poll(order_book, order_execution_strategy, remote_orders):
        order_book.get_open_orders.return_value = [
            Mock(identifier=remote.identifier, symbol="BTC/USDT") for remote in remote_orders
        ]
        by_id = {remote.identifier: remote for remote in remote_orders}
        order_execution_strategy.get_order = AsyncMock(side_effect=lambda order_id, _pair: by_id[order_id])

    @staticmethod
    def _record(order_update_handler):
        """Records the updates applied and the retries run, in the order the poll runs them."""
        calls = []
        order_update_handler.apply_order_update = AsyncMock(side_effect=lambda update: calls.append(update.identifier))
        order_update_handler.retry_pending_placements = AsyncMock(side_effect=lambda: calls.append("retry"))
        return calls

    async def test_the_fills_of_one_poll_are_applied_buys_high_to_low_then_sells_low_to_high(self, setup_tracker):
        tracker, order_book, order_execution_strategy, order_update_handler = setup_tracker
        calls = self._record(order_update_handler)
        self._poll(
            order_book,
            order_execution_strategy,
            [
                Mock(identifier="s120", status=OrderStatus.CLOSED, side=OrderSide.SELL, price=120.0),
                Mock(identifier="b90", status=OrderStatus.CLOSED, side=OrderSide.BUY, price=90.0),
                Mock(identifier="s110", status=OrderStatus.CANCELED, side=OrderSide.SELL, price=110.0),
                Mock(identifier="b100", status=OrderStatus.CLOSED, side=OrderSide.BUY, price=100.0),
                Mock(identifier="b80", status=OrderStatus.OPEN, side=OrderSide.BUY, price=80.0, filled=0),
            ],
        )

        await tracker._process_open_orders()

        assert calls == ["b100", "b90", "s110", "s120", "retry"]

    async def test_a_poll_ends_with_one_retry_of_the_pending_placements(self, setup_tracker):
        tracker, order_book, order_execution_strategy, order_update_handler = setup_tracker
        calls = self._record(order_update_handler)
        self._poll(
            order_book,
            order_execution_strategy,
            [Mock(identifier="b100", status=OrderStatus.CLOSED, side=OrderSide.BUY, price=100.0)],
        )

        await tracker._process_open_orders()

        assert calls == ["b100", "retry"]

    async def test_a_cancelled_status_fetch_does_not_stop_the_poll(self, setup_tracker):
        tracker, order_book, order_execution_strategy, order_update_handler = setup_tracker
        calls = self._record(order_update_handler)
        filled = Mock(identifier="b90", status=OrderStatus.CLOSED, side=OrderSide.BUY, price=90.0)
        order_book.get_open_orders.return_value = [
            Mock(identifier="lost", symbol="BTC/USDT"),
            Mock(identifier="b90", symbol="BTC/USDT"),
        ]

        async def get_order(order_id, _pair):
            if order_id == "lost":
                raise asyncio.CancelledError
            return filled

        order_execution_strategy.get_order = AsyncMock(side_effect=get_order)

        await tracker._process_open_orders()

        assert calls == ["b90", "retry"]

    async def test_a_poll_without_open_orders_still_retries(self, setup_tracker):
        tracker, order_book, _, order_update_handler = setup_tracker
        calls = self._record(order_update_handler)
        order_book.get_open_orders.return_value = []

        await tracker._process_open_orders()

        assert calls == ["retry"]

    async def test_a_failed_update_does_not_stop_the_rest_of_the_poll(self, setup_tracker, caplog):
        tracker, order_book, order_execution_strategy, order_update_handler = setup_tracker
        calls = self._record(order_update_handler)

        def apply(update):
            if update.identifier == "b100":
                raise RuntimeError("boom")
            calls.append(update.identifier)

        order_update_handler.apply_order_update = AsyncMock(side_effect=apply)
        self._poll(
            order_book,
            order_execution_strategy,
            [
                Mock(identifier="b100", status=OrderStatus.CLOSED, side=OrderSide.BUY, price=100.0),
                Mock(identifier="b90", status=OrderStatus.CLOSED, side=OrderSide.BUY, price=90.0),
            ],
        )

        await tracker._process_open_orders()

        assert calls == ["b90", "retry"]
        assert _messages(caplog, logging.ERROR) == ["Error during order processing: boom"]

    async def test_a_failing_retry_is_logged_and_the_poll_ends(self, setup_tracker, caplog):
        tracker, order_book, _, order_update_handler = setup_tracker
        order_book.get_open_orders.return_value = []
        order_update_handler.retry_pending_placements = AsyncMock(side_effect=RuntimeError("journal broken"))

        await tracker._process_open_orders()

        assert _messages(caplog, logging.ERROR) == ["Error while retrying the pending placements: journal broken"]

    async def test_a_poll_with_several_orders_runs_as_one_task(self, setup_tracker):
        tracker, order_book, order_execution_strategy, _ = setup_tracker
        order_book.get_open_orders.return_value = [
            Mock(identifier="order_1", symbol="BTC/USDT"),
            Mock(identifier="order_2", symbol="BTC/USDT"),
        ]
        started = asyncio.Event()

        async def stuck(*_args):
            started.set()
            await asyncio.Event().wait()

        order_execution_strategy.get_order = AsyncMock(side_effect=stuck)
        tracker.HANDLER_DRAIN_TIMEOUT = 0.01
        tracker.start_tracking()
        await asyncio.wait_for(started.wait(), timeout=1)

        assert len(tracker._active_tasks) == 1

        await asyncio.wait_for(tracker.stop_tracking(), timeout=1)
