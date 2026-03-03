from unittest.mock import AsyncMock, Mock, patch

import pytest

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.order_handling.order_status_tracker import OrderStatusTracker
from grid_trading_bot.core.services.exceptions import DataFetchError


class TestBuyFillSellPlacement:
    """OrderManager unit tests for the buy fill → sell placement cycle."""

    @pytest.mark.parametrize("strategy_label", ["simple_grid", "hedged_grid"])
    @pytest.mark.asyncio
    async def test_buy_fill_places_sell_order(self, setup_order_manager, strategy_label):
        manager, grid_manager, order_validator, balance_tracker, order_book, _, order_execution_strategy, _ = (
            setup_order_manager
        )
        buy_order = Mock(side=OrderSide.BUY, filled=0.01, price=50000)
        buy_grid_level = Mock(price=50000)
        sell_grid_level = Mock(price=52000)
        mock_sell_order = Mock(amount=0.01)

        order_book.get_grid_level_for_order.return_value = buy_grid_level
        grid_manager.get_paired_sell_level.return_value = sell_grid_level
        grid_manager.can_place_order.return_value = True
        order_validator.adjust_and_validate_sell_quantity.return_value = 0.01
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=mock_sell_order)

        await manager._on_order_filled(buy_order)

        grid_manager.complete_order.assert_called_once_with(buy_grid_level, OrderSide.BUY)
        grid_manager.get_paired_sell_level.assert_called_once_with(buy_grid_level)
        order_execution_strategy.execute_limit_order.assert_awaited_once_with(
            OrderSide.SELL,
            "BTC/USDT",
            0.01,
            52000,
        )
        order_book.add_order.assert_called_once_with(mock_sell_order, sell_grid_level)

    @pytest.mark.parametrize("strategy_label", ["simple_grid", "hedged_grid"])
    @pytest.mark.asyncio
    async def test_buy_fill_uses_filled_quantity_for_sell(self, setup_order_manager, strategy_label):
        manager, grid_manager, order_validator, _, order_book, _, order_execution_strategy, _ = setup_order_manager
        buy_order = Mock(side=OrderSide.BUY, filled=0.0075, amount=0.01, price=50000)
        buy_grid_level = Mock(price=50000)
        sell_grid_level = Mock(price=52000)

        order_book.get_grid_level_for_order.return_value = buy_grid_level
        grid_manager.get_paired_sell_level.return_value = sell_grid_level
        grid_manager.can_place_order.return_value = True
        order_validator.adjust_and_validate_sell_quantity.return_value = 0.0075
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=Mock(amount=0.0075))

        await manager._on_order_filled(buy_order)

        # Verify sell quantity comes from order.filled (0.0075), not order.amount (0.01)
        order_validator.adjust_and_validate_sell_quantity.assert_called_once_with(
            crypto_balance=manager.balance_tracker.crypto_balance,
            order_quantity=0.0075,
        )

    @pytest.mark.parametrize("strategy_label", ["simple_grid", "hedged_grid"])
    @pytest.mark.asyncio
    async def test_buy_fill_no_paired_sell_level(self, setup_order_manager, strategy_label):
        manager, grid_manager, _, _, order_book, _, order_execution_strategy, _ = setup_order_manager
        buy_order = Mock(side=OrderSide.BUY, filled=0.01, price=50000)
        buy_grid_level = Mock(price=50000)

        order_book.get_grid_level_for_order.return_value = buy_grid_level
        grid_manager.get_paired_sell_level.return_value = None
        order_execution_strategy.execute_limit_order = AsyncMock()

        with patch.object(manager.logger, "warning") as mock_warning:
            await manager._on_order_filled(buy_order)

            grid_manager.complete_order.assert_called_once_with(buy_grid_level, OrderSide.BUY)
            order_execution_strategy.execute_limit_order.assert_not_awaited()
            mock_warning.assert_called_once_with(
                f"No valid sell grid level found for buy grid level {buy_grid_level}. Skipping sell order placement.",
            )

    @pytest.mark.parametrize("strategy_label", ["simple_grid", "hedged_grid"])
    @pytest.mark.asyncio
    async def test_buy_fill_sell_level_cannot_place(self, setup_order_manager, strategy_label):
        manager, grid_manager, _, _, order_book, _, order_execution_strategy, _ = setup_order_manager
        buy_order = Mock(side=OrderSide.BUY, filled=0.01, price=50000)
        buy_grid_level = Mock(price=50000)
        sell_grid_level = Mock(price=52000)

        order_book.get_grid_level_for_order.return_value = buy_grid_level
        grid_manager.get_paired_sell_level.return_value = sell_grid_level
        grid_manager.can_place_order.return_value = False
        order_execution_strategy.execute_limit_order = AsyncMock()

        with patch.object(manager.logger, "warning") as mock_warning:
            await manager._on_order_filled(buy_order)

            grid_manager.complete_order.assert_called_once_with(buy_grid_level, OrderSide.BUY)
            order_execution_strategy.execute_limit_order.assert_not_awaited()
            mock_warning.assert_called_once_with(
                f"No valid sell grid level found for buy grid level {buy_grid_level}. Skipping sell order placement.",
            )


class TestCcxtResponseHandling:
    """OrderStatusTracker + LiveOrderExecutionStrategy._parse_order_result tests."""

    @pytest.mark.asyncio
    async def test_partial_fill_no_event_published(self, setup_tracker):
        tracker, _, _, event_bus = setup_tracker
        mock_remote_order = Mock(
            identifier="order_123",
            status=OrderStatus.OPEN,
            filled=0.3,
            remaining=0.7,
        )

        with patch.object(tracker.logger, "info") as mock_logger_info:
            await tracker._handle_order_status_change(mock_remote_order)

            event_bus.publish.assert_not_called()
            mock_logger_info.assert_called_once_with(
                f"Order {mock_remote_order} partially filled. Filled: {mock_remote_order.filled}, "
                f"Remaining: {mock_remote_order.remaining}.",
            )

    @pytest.mark.asyncio
    async def test_canceled_order_publishes_cancelled_event(self, setup_tracker):
        tracker, order_book, _, event_bus = setup_tracker
        mock_remote_order = Mock(identifier="order_123", status=OrderStatus.CANCELED)
        event_bus.publish = AsyncMock()

        await tracker._handle_order_status_change(mock_remote_order)

        order_book.update_order_status.assert_called_once_with("order_123", OrderStatus.CANCELED)
        event_bus.publish.assert_awaited_once_with(Events.ORDER_CANCELLED, mock_remote_order)

    @pytest.mark.asyncio
    async def test_expired_order_falls_to_unhandled(self, setup_tracker):
        tracker, _, _, event_bus = setup_tracker
        mock_remote_order = Mock(identifier="order_123", status=OrderStatus.EXPIRED)

        with patch.object(tracker.logger, "warning") as mock_logger_warning:
            await tracker._handle_order_status_change(mock_remote_order)

            event_bus.publish.assert_not_called()
            mock_logger_warning.assert_called_once_with(
                f"Unhandled order status '{OrderStatus.EXPIRED}' for order order_123.",
            )

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


class TestEventDrivenBuySellFlow:
    """Integration test using a real EventBus for the tracker → event → manager → sell chain."""

    @pytest.fixture
    def setup_event_driven_flow(self):
        event_bus = EventBus()
        order_book = Mock()

        # Tracker dependencies
        tracker_execution_strategy = Mock()
        tracker = OrderStatusTracker(
            order_book=order_book,
            order_execution_strategy=tracker_execution_strategy,
            event_bus=event_bus,
            polling_interval=1.0,
        )

        # Manager dependencies
        grid_manager = Mock()
        grid_manager.buy_ratio = 1.0
        grid_manager.sell_ratio = 1.0
        order_validator = Mock()
        balance_tracker = Mock()
        balance_tracker.reserve_funds_for_buy = AsyncMock()
        balance_tracker.reserve_funds_for_sell = AsyncMock()
        manager_execution_strategy = Mock()
        notification_handler = Mock()
        notification_handler.async_send_notification = AsyncMock()
        order_simulator = Mock()

        manager = OrderManager(
            grid_manager=grid_manager,
            order_validator=order_validator,
            balance_tracker=balance_tracker,
            order_book=order_book,
            event_bus=event_bus,
            order_execution_strategy=manager_execution_strategy,
            notification_handler=notification_handler,
            order_simulator=order_simulator,
            trading_mode=TradingMode.LIVE,
            trading_pair="BTC/USDT",
        )

        return (
            tracker,
            manager,
            order_book,
            tracker_execution_strategy,
            manager_execution_strategy,
            grid_manager,
            order_validator,
            balance_tracker,
        )

    @pytest.mark.asyncio
    async def test_tracker_detects_fill_and_triggers_sell_placement(self, setup_event_driven_flow):
        (
            tracker,
            manager,
            order_book,
            tracker_exec,
            manager_exec,
            grid_manager,
            order_validator,
            balance_tracker,
        ) = setup_event_driven_flow

        # Local order for tracker to discover
        local_order = Mock(identifier="order_123", symbol="BTC/USDT", status=OrderStatus.OPEN)
        order_book.get_open_orders.return_value = [local_order]

        # Remote order returned by exchange (CLOSED / filled buy)
        remote_order = Mock(
            identifier="order_123",
            symbol="BTC/USDT",
            status=OrderStatus.CLOSED,
            side=OrderSide.BUY,
            filled=0.01,
            price=50000,
        )
        tracker_exec.get_order = AsyncMock(return_value=remote_order)

        # Manager: configure order_book to return grid level for the filled order
        buy_grid_level = Mock(price=50000)
        sell_grid_level = Mock(price=52000)
        order_book.get_grid_level_for_order.return_value = buy_grid_level

        grid_manager.get_paired_sell_level.return_value = sell_grid_level
        grid_manager.can_place_order.return_value = True
        order_validator.adjust_and_validate_sell_quantity.return_value = 0.01

        mock_sell_order = Mock(amount=0.01)
        manager_exec.execute_limit_order = AsyncMock(return_value=mock_sell_order)

        # Trigger the full chain: tracker → EventBus → manager
        await tracker._process_open_orders()

        # Verify tracker detected the fill
        tracker_exec.get_order.assert_awaited_once_with("order_123", "BTC/USDT")
        order_book.update_order_status.assert_called_once_with("order_123", OrderStatus.CLOSED)

        # Verify manager received the event and placed the sell order
        grid_manager.complete_order.assert_called_once_with(buy_grid_level, OrderSide.BUY)
        manager_exec.execute_limit_order.assert_awaited_once_with(
            OrderSide.SELL,
            "BTC/USDT",
            0.01,
            52000,
        )
