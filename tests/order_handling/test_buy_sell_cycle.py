from unittest.mock import AsyncMock, Mock, patch

import pytest

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.order_handling.order_status_tracker import OrderStatusTracker
from grid_trading_bot.core.services.exceptions import DataFetchError


class TestBuyFillSellPlacement:
    """OrderManager unit tests for the buy fill → sell placement cycle."""

    @pytest.mark.parametrize("strategy_label", ["simple_grid", "hedged_grid"])
    @pytest.mark.asyncio
    async def test_buy_fill_places_sell_order(self, setup_order_manager, strategy_label):
        manager, grid_manager, order_validator, _balance_tracker, order_book, order_execution_strategy, _ = (
            setup_order_manager
        )
        buy_order = Mock(side=OrderSide.BUY, filled=0.01, price=50000)
        buy_grid_level = Mock(price=50000)
        sell_grid_level = Mock(price=52000)
        mock_sell_order = Mock(amount=0.01)

        order_book.get_grid_level_for_order.return_value = buy_grid_level
        grid_manager.get_sell_target.return_value = sell_grid_level
        order_validator.adjust_and_validate_sell_quantity.return_value = 0.01
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=mock_sell_order)

        await manager._handle_order_completion(buy_order, buy_grid_level)

        grid_manager.complete_order.assert_called_once_with(buy_grid_level, OrderSide.BUY, False)
        grid_manager.get_sell_target.assert_called_once_with(buy_grid_level)
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
        manager, grid_manager, order_validator, _, order_book, order_execution_strategy, _ = setup_order_manager
        buy_order = Mock(side=OrderSide.BUY, filled=0.0075, amount=0.01, price=50000)
        buy_grid_level = Mock(price=50000)
        sell_grid_level = Mock(price=52000)

        order_book.get_grid_level_for_order.return_value = buy_grid_level
        grid_manager.get_sell_target.return_value = sell_grid_level
        order_validator.adjust_and_validate_sell_quantity.return_value = 0.0075
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=Mock(amount=0.0075))

        await manager._handle_order_completion(buy_order, buy_grid_level)

        # Verify sell quantity comes from order.filled (0.0075), not order.amount (0.01)
        order_validator.adjust_and_validate_sell_quantity.assert_called_once_with(
            crypto_balance=manager.balance_tracker.crypto_balance,
            order_quantity=0.0075,
        )

    @pytest.mark.parametrize("strategy_label", ["simple_grid", "hedged_grid"])
    @pytest.mark.asyncio
    async def test_buy_fill_without_sell_target_places_nothing(self, setup_order_manager, strategy_label):
        manager, grid_manager, _, _, order_book, order_execution_strategy, _ = setup_order_manager
        buy_order = Mock(side=OrderSide.BUY, filled=0.01, price=50000)
        buy_grid_level = Mock(price=50000)

        order_book.get_grid_level_for_order.return_value = buy_grid_level
        grid_manager.get_sell_target.return_value = None
        order_execution_strategy.execute_limit_order = AsyncMock()

        await manager._handle_order_completion(buy_order, buy_grid_level)

        grid_manager.complete_order.assert_called_once_with(buy_grid_level, OrderSide.BUY, False)
        order_execution_strategy.execute_limit_order.assert_not_awaited()

    @pytest.mark.parametrize("strategy_label", ["simple_grid", "hedged_grid"])
    @pytest.mark.asyncio
    async def test_buy_fill_trusts_the_sell_target(self, setup_order_manager, strategy_label):
        manager, grid_manager, order_validator, _, order_book, order_execution_strategy, _ = setup_order_manager
        buy_order = Mock(side=OrderSide.BUY, filled=0.01, price=50000)
        buy_grid_level = Mock(price=50000)
        sell_grid_level = Mock(price=52000)

        order_book.get_grid_level_for_order.return_value = buy_grid_level
        grid_manager.get_sell_target.return_value = sell_grid_level
        grid_manager.can_place_order.return_value = False
        order_validator.adjust_and_validate_sell_quantity.return_value = 0.01
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=Mock(amount=0.01))

        await manager._handle_order_completion(buy_order, buy_grid_level)

        order_execution_strategy.execute_limit_order.assert_awaited_once_with(OrderSide.SELL, "BTC/USDT", 0.01, 52000)


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


class TestEventDrivenBuySellFlow:
    """Integration test of the tracker → manager → sell chain."""

    @pytest.fixture
    def setup_event_driven_flow(self):
        order_book = Mock()
        order_book.has_open_orders_at.return_value = False

        # Tracker dependencies; the manager, built below, receives the updates
        update_handler = Mock()
        tracker_execution_strategy = Mock()
        tracker = OrderStatusTracker(
            order_book=order_book,
            order_execution_strategy=tracker_execution_strategy,
            order_update_handler=update_handler,
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
        balance_tracker.record_execution = AsyncMock()
        manager_execution_strategy = Mock()
        notification_handler = Mock()
        notification_handler.async_send_notification = AsyncMock()

        manager = OrderManager(
            grid_manager=grid_manager,
            order_validator=order_validator,
            balance_tracker=balance_tracker,
            order_book=order_book,
            order_execution_strategy=manager_execution_strategy,
            notification_handler=notification_handler,
            trading_mode=TradingMode.LIVE,
            trading_pair="BTC/USDT",
        )
        update_handler.apply_order_update = manager.apply_order_update

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
            _manager,
            order_book,
            tracker_exec,
            manager_exec,
            grid_manager,
            order_validator,
            _balance_tracker,
        ) = setup_event_driven_flow

        # Local order for tracker to discover
        local_order = Mock(identifier="order_123", symbol="BTC/USDT", status=OrderStatus.OPEN)
        order_book.get_open_orders.return_value = [local_order]
        order_book.get_order.return_value = Mock(
            identifier="order_123",
            status=OrderStatus.OPEN,
            side=OrderSide.BUY,
            filled=0.01,
            is_open=Mock(return_value=True),
            apply_update=Mock(return_value=0.01),
        )

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

        grid_manager.get_sell_target.return_value = sell_grid_level
        order_validator.adjust_and_validate_sell_quantity.return_value = 0.01

        mock_sell_order = Mock(amount=0.01)
        manager_exec.execute_limit_order = AsyncMock(return_value=mock_sell_order)

        # Trigger the full chain: tracker → manager
        await tracker._process_open_orders()

        # Verify tracker detected the fill
        tracker_exec.get_order.assert_awaited_once_with("order_123", "BTC/USDT")

        # Verify manager received the update and placed the sell order
        grid_manager.complete_order.assert_called_once_with(buy_grid_level, OrderSide.BUY, False)
        manager_exec.execute_limit_order.assert_awaited_once_with(
            OrderSide.SELL,
            "BTC/USDT",
            0.01,
            52000,
        )
