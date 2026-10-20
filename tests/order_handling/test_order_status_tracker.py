import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest

from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus
from grid_trading_bot.core.services.exceptions import DataFetchError


class TestOrderStatusTracker:
    @pytest.mark.asyncio
    async def test_process_open_orders_success(self, setup_tracker):
        tracker, order_book, order_execution_strategy, _ = setup_tracker
        mock_order = Mock(identifier="order_1", symbol="BTC/USDT", status=OrderStatus.OPEN)
        mock_remote_order = Mock(identifier="order_1", symbol="BTC/USDT", status=OrderStatus.CLOSED)

        order_book.get_open_orders.return_value = [mock_order]
        order_execution_strategy.get_order = AsyncMock(return_value=mock_remote_order)
        tracker._handle_order_status_change = AsyncMock()

        await tracker._process_open_orders()

        order_execution_strategy.get_order.assert_awaited_once_with("order_1", "BTC/USDT")
        tracker._handle_order_status_change.assert_awaited_once_with(mock_remote_order)

    @pytest.mark.asyncio
    async def test_process_open_orders_failure(self, setup_tracker):
        tracker, order_book, order_execution_strategy, _ = setup_tracker
        mock_order = Mock(identifier="order_1", symbol="BTC/USDT", status=OrderStatus.OPEN)

        order_book.get_open_orders.return_value = [mock_order]
        order_execution_strategy.get_order = AsyncMock(side_effect=DataFetchError("Failed to fetch order"))

        with patch.object(tracker.logger, "error") as mock_logger_error:
            await tracker._process_open_orders()

            order_execution_strategy.get_order.assert_awaited_once_with("order_1", "BTC/USDT")
            mock_logger_error.assert_called_once_with(
                "Failed to query remote order with identifier order_1: Failed to fetch order",
                exc_info=True,
            )

    @pytest.mark.asyncio
    async def test_handle_order_status_change_closed_hands_the_fill_over(self, setup_tracker):
        tracker, _, _, _ = setup_tracker
        mock_remote_order = Mock(identifier="order_1", status=OrderStatus.CLOSED)

        with patch.object(tracker.logger, "info") as mock_logger_info:
            await tracker._handle_order_status_change(mock_remote_order)

        tracker.order_update_handler.apply_order_update.assert_awaited_once_with(mock_remote_order)
        mock_logger_info.assert_called_once_with("Order order_1 filled.")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED])
    async def test_an_ended_order_is_handed_over(self, setup_tracker, status):
        tracker, _, _, order_update_handler = setup_tracker
        mock_remote_order = Mock(identifier="order_1", status=status)

        with patch.object(tracker.logger, "warning") as mock_logger_warning:
            await tracker._handle_order_status_change(mock_remote_order)

        order_update_handler.apply_order_update.assert_awaited_once_with(mock_remote_order)
        mock_logger_warning.assert_any_call(f"Order order_1 ended as {status.value}.")

    @pytest.mark.asyncio
    async def test_handle_order_status_change_unknown_status(self, setup_tracker):
        tracker, _, _, _ = setup_tracker
        mock_remote_order = Mock(identifier="order_1", status=OrderStatus.UNKNOWN)

        with patch.object(tracker.logger, "error") as mock_logger_error:
            await tracker._handle_order_status_change(mock_remote_order)

            mock_logger_error.assert_any_call(
                f"Missing 'status' in remote order object: {mock_remote_order}",
                exc_info=True,
            )
            mock_logger_error.assert_any_call(
                "Error handling order status change: Order data from the exchange is missing the 'status' field.",
                exc_info=True,
            )
            assert mock_logger_error.call_count == 2

    @pytest.mark.asyncio
    async def test_handle_order_status_change_open(self, setup_tracker):
        tracker, _, _, _ = setup_tracker
        mock_remote_order = Mock(identifier="order_1", status=OrderStatus.OPEN, filled=0)

        with patch.object(tracker.logger, "info") as mock_logger_info:
            await tracker._handle_order_status_change(mock_remote_order)

            mock_logger_info.assert_called_once_with(f"Order {mock_remote_order} is still open. No fills yet.")

    @pytest.mark.asyncio
    async def test_handle_order_status_change_partially_filled(self, setup_tracker):
        tracker, _, _, _ = setup_tracker
        mock_remote_order = Mock(identifier="order_1", status=OrderStatus.OPEN, filled=0.5, remaining=0.5)

        with patch.object(tracker.logger, "info") as mock_logger_info:
            await tracker._handle_order_status_change(mock_remote_order)

            mock_logger_info.assert_called_once_with(
                f"Order {mock_remote_order} partially filled. Filled: {mock_remote_order.filled}, "
                f"Remaining: {mock_remote_order.remaining}.",
            )

    @pytest.mark.asyncio
    async def test_handle_order_status_change_unhandled_status(self, setup_tracker):
        tracker, _, _, _ = setup_tracker
        mock_remote_order = Mock(identifier="order_1", status="unexpected_status")

        with patch.object(tracker.logger, "warning") as mock_logger_warning:
            await tracker._handle_order_status_change(mock_remote_order)

            mock_logger_warning.assert_called_once_with("Unhandled order status 'unexpected_status' for order order_1.")

    @pytest.mark.asyncio
    async def test_start_tracking_creates_monitoring_task(self, setup_tracker):
        tracker, _, _, _ = setup_tracker

        tracker.start_tracking()
        assert tracker._monitoring_task is not None
        assert not tracker._monitoring_task.done()

        await tracker.stop_tracking()

    @pytest.mark.asyncio
    async def test_start_tracking_warns_if_already_running(self, setup_tracker):
        tracker, _, _, _ = setup_tracker

        tracker.start_tracking()
        with patch.object(tracker.logger, "warning") as mock_logger_warning:
            tracker.start_tracking()
            mock_logger_warning.assert_called_once_with("OrderStatusTracker is already running.")

        await tracker.stop_tracking()

    @pytest.mark.asyncio
    async def test_stop_tracking_cancels_monitoring_task(self, setup_tracker):
        tracker, _, _, _ = setup_tracker

        tracker.start_tracking()
        assert tracker._monitoring_task is not None

        await tracker.stop_tracking()
        assert tracker._monitoring_task is None

    @pytest.mark.asyncio
    async def test_track_open_order_statuses_handles_cancellation(self, setup_tracker):
        tracker, _, _, _ = setup_tracker

        tracker.start_tracking()
        await asyncio.sleep(0.1)

        await tracker.stop_tracking()

        assert tracker._monitoring_task is None

    @pytest.mark.asyncio
    async def test_track_open_order_statuses_handles_unexpected_error(self, setup_tracker):
        tracker, order_book, _, _ = setup_tracker

        order_book.get_open_orders.side_effect = Exception("Unexpected error")
        monitoring_task = asyncio.create_task(tracker._track_open_order_statuses())

        await asyncio.sleep(0.1)

        monitoring_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitoring_task

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
    async def test_create_task_adds_to_active_tasks(self, setup_tracker):
        tracker, _, _, _ = setup_tracker

        async def dummy_coro():
            await asyncio.sleep(0.1)

        task = tracker._create_task(dummy_coro())

        assert task in tracker._active_tasks
        await task
        assert task not in tracker._active_tasks

    @pytest.mark.asyncio
    async def test_process_open_orders_with_multiple_orders(self, setup_tracker):
        tracker, order_book, order_execution_strategy, _ = setup_tracker

        mock_order1 = Mock(identifier="order_1", symbol="BTC/USDT", status=OrderStatus.OPEN)
        mock_order2 = Mock(identifier="order_2", symbol="ETH/USDT", status=OrderStatus.OPEN)

        mock_remote_order1 = Mock(
            identifier="order_1", symbol="BTC/USDT", status=OrderStatus.CLOSED, side=OrderSide.BUY, price=100.0
        )
        mock_remote_order2 = Mock(
            identifier="order_2", symbol="ETH/USDT", status=OrderStatus.CANCELED, side=OrderSide.SELL, price=110.0
        )

        order_book.get_open_orders.return_value = [mock_order1, mock_order2]
        order_execution_strategy.get_order = AsyncMock(side_effect=[mock_remote_order1, mock_remote_order2])
        tracker._handle_order_status_change = AsyncMock()

        await tracker._process_open_orders()

        tracker._handle_order_status_change.assert_any_call(mock_remote_order1)
        tracker._handle_order_status_change.assert_any_call(mock_remote_order2)


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

    async def test_the_fills_of_one_poll_are_applied_buys_high_to_low_then_sells_low_to_high(self, setup_tracker):
        tracker, order_book, order_execution_strategy, order_update_handler = setup_tracker
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

        applied = [c.args[0].identifier for c in order_update_handler.apply_order_update.await_args_list]
        assert applied == ["b100", "b90", "s110", "s120"]

    async def test_a_poll_ends_with_one_retry_of_the_pending_placements(self, setup_tracker):
        tracker, order_book, order_execution_strategy, order_update_handler = setup_tracker
        calls = []
        order_update_handler.apply_order_update = AsyncMock(side_effect=lambda update: calls.append(update.identifier))
        order_update_handler.retry_pending_placements = AsyncMock(side_effect=lambda: calls.append("retry"))
        self._poll(
            order_book,
            order_execution_strategy,
            [Mock(identifier="b100", status=OrderStatus.CLOSED, side=OrderSide.BUY, price=100.0)],
        )

        await tracker._process_open_orders()

        assert calls == ["b100", "retry"]

    async def test_a_poll_without_open_orders_still_retries(self, setup_tracker):
        tracker, order_book, _, order_update_handler = setup_tracker
        order_book.get_open_orders.return_value = []

        await tracker._process_open_orders()

        order_update_handler.retry_pending_placements.assert_awaited_once()

    async def test_a_failed_update_does_not_stop_the_rest_of_the_poll(self, setup_tracker):
        tracker, order_book, order_execution_strategy, order_update_handler = setup_tracker
        applied = []

        def apply(update):
            if update.identifier == "b100":
                raise RuntimeError("boom")
            applied.append(update.identifier)

        order_update_handler.apply_order_update = AsyncMock(side_effect=apply)
        self._poll(
            order_book,
            order_execution_strategy,
            [
                Mock(identifier="b100", status=OrderStatus.CLOSED, side=OrderSide.BUY, price=100.0),
                Mock(identifier="b90", status=OrderStatus.CLOSED, side=OrderSide.BUY, price=90.0),
            ],
        )

        with patch.object(tracker.logger, "error") as mock_logger_error:
            await tracker._process_open_orders()

        assert applied == ["b90"]
        mock_logger_error.assert_called_once()
        order_update_handler.retry_pending_placements.assert_awaited_once()

    async def test_a_failing_retry_is_logged_and_the_poll_ends(self, setup_tracker):
        tracker, order_book, _, order_update_handler = setup_tracker
        order_book.get_open_orders.return_value = []
        order_update_handler.retry_pending_placements = AsyncMock(side_effect=RuntimeError("journal broken"))

        with patch.object(tracker.logger, "error") as mock_logger_error:
            await tracker._process_open_orders()

        mock_logger_error.assert_called_once_with(
            "Error while retrying the pending placements: journal broken", exc_info=True
        )

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
