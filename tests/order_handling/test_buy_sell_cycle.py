from unittest.mock import Mock, patch

import pytest
from tests.fakes.trading_stack import ARMING_PRICE, build_stack

from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus
from grid_trading_bot.core.order_handling.order_status_tracker import OrderStatusTracker
from grid_trading_bot.core.services.exceptions import DataFetchError


class TestCcxtResponseHandling:
    """OrderStatusTracker + LiveOrderExecutionStrategy._parse_order_result tests."""

    @pytest.mark.asyncio
    async def test_partial_fill_no_event_published(self, setup_tracker):
        tracker, _, _, order_update_handler = setup_tracker
        mock_remote_order = Mock(
            identifier="order_123",
            status=OrderStatus.OPEN,
            filled=0.3,
            remaining=0.7,
        )

        with patch.object(tracker.logger, "info") as mock_logger_info:
            await tracker._handle_order_status_change(mock_remote_order)

            order_update_handler.apply_order_update.assert_not_awaited()
            mock_logger_info.assert_called_once_with(
                f"Order {mock_remote_order} partially filled. Filled: {mock_remote_order.filled}, "
                f"Remaining: {mock_remote_order.remaining}.",
            )

    @pytest.mark.asyncio
    async def test_canceled_order_is_handed_to_the_update_handler(self, setup_tracker):
        tracker, _, _, order_update_handler = setup_tracker
        mock_remote_order = Mock(identifier="order_123", status=OrderStatus.CANCELED)

        await tracker._handle_order_status_change(mock_remote_order)

        order_update_handler.apply_order_update.assert_awaited_once_with(mock_remote_order)

    @pytest.mark.parametrize("missing_field", ["id", "status", "type", "side"])
    @pytest.mark.asyncio
    async def test_missing_required_fields_raises_error(self, setup_live_strategy, missing_field):
        strategy, _ = setup_live_strategy
        raw_order = {
            "id": "order_123",
            "status": "closed",
            "type": "limit",
            "side": "buy",
            "price": 50000,
            "amount": 0.01,
        }
        del raw_order[missing_field]

        with pytest.raises(DataFetchError, match=f"Exchange response missing required fields: {missing_field}"):
            await strategy._parse_order_result(raw_order)

    @pytest.mark.asyncio
    async def test_unknown_status_logs_error(self, setup_tracker):
        tracker, _, _, _ = setup_tracker
        mock_remote_order = Mock(identifier="order_123", status=OrderStatus.UNKNOWN)

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
    async def test_unparseable_status_raises_value_error(self, setup_live_strategy):
        strategy, _ = setup_live_strategy
        raw_order = {
            "id": "order_123",
            "status": "foobar",
            "type": "limit",
            "side": "buy",
        }

        with pytest.raises(ValueError, match="'foobar' is not a valid OrderStatus"):
            await strategy._parse_order_result(raw_order)


class TestTrackerToOrderManagerFlow:
    """The tracker hands a fill it detects to the order manager, which places the paired sell."""

    async def test_tracker_detects_fill_and_triggers_sell_placement(self):
        stack = build_stack()
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        tracker = OrderStatusTracker(stack.book, stack.execution, stack.manager, polling_interval=1.0)
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        stack.execution.fill(buy.identifier)

        await tracker._process_open_orders()

        sell = stack.execution.order_at(110.0, OrderSide.SELL)
        assert sell.amount == pytest.approx(buy.amount * 0.999)
        assert stack.book.get_order(sell.identifier).is_open()
        assert stack.book.get_order(buy.identifier).is_filled()
