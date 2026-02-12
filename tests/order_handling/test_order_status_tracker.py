import asyncio
import contextlib
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.bot_management.event_bus import Events
from core.order_handling.order import OrderStatus
from core.order_handling.order_status_tracker import OrderStatusTracker


class TestOrderStatusTracker:
    @pytest.fixture
    def setup_tracker(self):
        order_book = Mock()
        order_execution_strategy = Mock()
        event_bus = Mock()
        tracker = OrderStatusTracker(
            order_book=order_book,
            order_execution_strategy=order_execution_strategy,
            event_bus=event_bus,
            polling_interval=1.0,
        )
        return tracker, order_book, order_execution_strategy, event_bus

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
        order_execution_strategy.get_order = AsyncMock(side_effect=Exception("Failed to fetch order"))

        with patch.object(tracker.logger, "error") as mock_logger_error:
            await tracker._process_open_orders()

            order_execution_strategy.get_order.assert_awaited_once_with("order_1", "BTC/USDT")
            mock_logger_error.assert_called_once_with(
                "Failed to query remote order with identifier order_1: Failed to fetch order",
                exc_info=True,
            )

    @pytest.mark.asyncio
    async def test_handle_order_status_change_closed(self, setup_tracker):
        tracker, order_book, _, event_bus = setup_tracker
        mock_remote_order = Mock(identifier="order_1", status=OrderStatus.CLOSED)
        event_bus.publish = AsyncMock()

        with patch.object(tracker.logger, "info") as mock_logger_info:
            await tracker._handle_order_status_change(mock_remote_order)

            order_book.update_order_status.assert_called_once_with("order_1", OrderStatus.CLOSED)
            event_bus.publish.assert_awaited_once_with(Events.ORDER_FILLED, mock_remote_order)
            mock_logger_info.assert_called_once_with("Order order_1 filled.")

    @pytest.mark.asyncio
    async def test_handle_order_status_change_canceled(self, setup_tracker):
        tracker, order_book, _, event_bus = setup_tracker
        mock_remote_order = Mock(identifier="order_1", status=OrderStatus.CANCELED)
        event_bus.publish = AsyncMock()

        with patch.object(tracker.logger, "warning") as mock_logger_warning:
            await tracker._handle_order_status_change(mock_remote_order)

            order_book.update_order_status.assert_called_once_with("order_1", OrderStatus.CANCELED)
            event_bus.publish.assert_awaited_once_with(Events.ORDER_CANCELLED, mock_remote_order)

            mock_logger_warning.assert_any_call("Order order_1 was canceled.")

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

        mock_remote_order1 = Mock(identifier="order_1", symbol="BTC/USDT", status=OrderStatus.CLOSED)
        mock_remote_order2 = Mock(identifier="order_2", symbol="ETH/USDT", status=OrderStatus.CANCELED)

        order_book.get_open_orders.return_value = [mock_order1, mock_order2]
        order_execution_strategy.get_order = AsyncMock(side_effect=[mock_remote_order1, mock_remote_order2])
        tracker._handle_order_status_change = AsyncMock()

        await tracker._process_open_orders()

        tracker._handle_order_status_change.assert_any_call(mock_remote_order1)
        tracker._handle_order_status_change.assert_any_call(mock_remote_order2)
