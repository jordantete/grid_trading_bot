from unittest.mock import AsyncMock, Mock

import pytest

from config.trading_mode import TradingMode
from core.bot_management.event_bus import EventBus
from core.bot_management.notification.notification_content import NotificationType
from core.domain.strategy_type import StrategyType
from core.order_handling.exceptions import OrderExecutionFailedError
from core.order_handling.order import OrderSide, OrderType
from core.order_handling.order_manager import OrderManager


class TestOrderManager:
    @pytest.fixture
    def setup_order_manager(self):
        grid_manager = Mock()
        order_validator = Mock()
        balance_tracker = Mock()
        order_book = Mock()
        event_bus = Mock(spec=EventBus)
        order_execution_strategy = Mock()
        notification_handler = Mock()
        notification_handler.async_send_notification = AsyncMock()
        order_simulator = Mock()
        order_simulator._simulate_fill = AsyncMock()

        manager = OrderManager(
            grid_manager=grid_manager,
            order_validator=order_validator,
            balance_tracker=balance_tracker,
            order_book=order_book,
            event_bus=event_bus,
            order_execution_strategy=order_execution_strategy,
            notification_handler=notification_handler,
            order_simulator=order_simulator,
            trading_mode=TradingMode.LIVE,
            trading_pair="BTC/USD",
            strategy_type=StrategyType.HEDGED_GRID,
        )
        return (
            manager,
            grid_manager,
            order_validator,
            balance_tracker,
            order_book,
            event_bus,
            order_execution_strategy,
            notification_handler,
        )

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_buy_orders(self, setup_order_manager):
        manager, grid_manager, order_validator, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.sorted_buy_grids = [50000, 49000, 48000]
        grid_manager.sorted_sell_grids = []
        grid_manager.grid_levels = {50000: Mock(), 49000: Mock(), 48000: Mock()}
        grid_manager.can_place_order.side_effect = lambda level, side: side == OrderSide.BUY
        order_validator.adjust_and_validate_buy_quantity.return_value = 0.01
        balance_tracker.balance = 1000
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=Mock())

        await manager.initialize_grid_orders(49500)

        grid_manager.can_place_order.assert_called()
        assert order_execution_strategy.execute_limit_order.call_count == 2

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_sell_orders(self, setup_order_manager):
        manager, grid_manager, order_validator, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.sorted_sell_grids = [52000, 53000, 54000]
        grid_manager.sorted_buy_grids = []
        grid_manager.grid_levels = {52000: Mock(), 53000: Mock(), 54000: Mock()}
        grid_manager.can_place_order.side_effect = lambda level, side: side == OrderSide.SELL
        order_validator.adjust_and_validate_sell_quantity.return_value = 0.01
        balance_tracker.crypto_balance = 1
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=Mock())

        await manager.initialize_grid_orders(51500)

        grid_manager.can_place_order.assert_called()
        assert order_execution_strategy.execute_limit_order.call_count == 3

    @pytest.mark.asyncio
    async def test_on_order_filled(self, setup_order_manager):
        manager, _, _, _, order_book, _, _, _ = setup_order_manager
        mock_order = Mock(side=OrderSide.BUY, price=50000)
        mock_grid_level = Mock()
        order_book.get_grid_level_for_order.return_value = mock_grid_level
        manager._handle_order_completion = AsyncMock()

        await manager._on_order_filled(mock_order)

        order_book.get_grid_level_for_order.assert_called_once_with(mock_order)
        manager._handle_order_completion.assert_awaited_once_with(mock_order, mock_grid_level)

    @pytest.mark.asyncio
    async def test_on_order_filled_no_grid_level(self, setup_order_manager):
        manager, _, _, _, order_book, _, _, _ = setup_order_manager
        mock_order = Mock()

        order_book.get_grid_level_for_order.return_value = None

        await manager._on_order_filled(mock_order)

        order_book.get_grid_level_for_order.assert_called_once_with(mock_order)

    @pytest.mark.asyncio
    async def test_handle_order_completion_buy(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        mock_order = Mock(side=OrderSide.BUY, filled=0.01)
        mock_grid_level = Mock(price=50000)
        grid_manager.get_paired_sell_level.return_value = Mock()
        grid_manager.can_place_order.return_value = True
        manager._place_order = AsyncMock()

        await manager._handle_order_completion(mock_order, mock_grid_level)

        manager._place_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_order_completion_sell(self, setup_order_manager):
        manager, _, _, _, _, _, _, _ = setup_order_manager
        mock_order = Mock(side=OrderSide.SELL, filled=0.01)
        mock_grid_level = Mock(price=50000)
        manager._get_or_create_paired_buy_level = Mock(return_value=Mock())
        manager._place_order = AsyncMock()

        await manager._handle_order_completion(mock_order, mock_grid_level)

        manager._place_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_perform_initial_purchase(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.get_initial_order_quantity.return_value = 0.01
        order_execution_strategy.execute_market_order = AsyncMock(return_value=Mock())

        await manager.perform_initial_purchase(50000)

        order_execution_strategy.execute_market_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_take_profit_or_stop_loss_order(self, setup_order_manager):
        manager, _, _, balance_tracker, _, _, order_execution_strategy, notification_handler = setup_order_manager
        balance_tracker.crypto_balance = 0.5
        order_execution_strategy.execute_market_order = AsyncMock(return_value=Mock())
        notification_handler.async_send_notification = AsyncMock()

        await manager.execute_take_profit_or_stop_loss_order(55000, take_profit_order=True)

        order_execution_strategy.execute_market_order.assert_awaited_once_with(
            OrderSide.SELL,
            manager.trading_pair,
            0.5,
            55000,
        )
        notification_handler.async_send_notification.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_execution_failed(self, setup_order_manager):
        (
            manager,
            grid_manager,
            order_validator,
            balance_tracker,
            _,
            _,
            order_execution_strategy,
            notification_handler,
        ) = setup_order_manager

        # Setup mocks
        grid_manager.sorted_buy_grids = [48000]
        grid_manager.sorted_sell_grids = []
        grid_manager.grid_levels = {48000: Mock()}
        grid_manager.can_place_order.return_value = True
        grid_manager.get_order_size_for_grid_level.return_value = 0.1
        order_validator.adjust_and_validate_buy_quantity.return_value = 0.1
        balance_tracker.get_total_balance_value.return_value = 50000
        order_execution_strategy.execute_limit_order.side_effect = OrderExecutionFailedError(
            "Test error",
            OrderSide.BUY,
            OrderType.LIMIT,
            "BTC/USDT",
            1,
            1000,
        )
        notification_handler.async_send_notification = AsyncMock()

        # Execute test
        await manager.initialize_grid_orders(50000)

        # Verify notifications were sent
        notification_handler.async_send_notification.assert_awaited_with(
            NotificationType.ORDER_FAILED,
            error_details="Error while placing initial buy order. Test error",
        )

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_insufficient_balance(self, setup_order_manager):
        manager, grid_manager, order_validator, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.sorted_buy_grids = [49000]
        grid_manager.sorted_sell_grids = []
        grid_manager.grid_levels = {49000: Mock()}
        grid_manager.can_place_order.return_value = True
        order_validator.adjust_and_validate_buy_quantity.side_effect = ValueError("Insufficient balance")
        balance_tracker.balance = 0  # Simulate insufficient balance
        order_execution_strategy.execute_limit_order = AsyncMock()

        await manager.initialize_grid_orders(49500)

        order_execution_strategy.execute_limit_order.assert_not_awaited()
        grid_manager.can_place_order.assert_called_once_with(grid_manager.grid_levels[49000], OrderSide.BUY)
        order_validator.adjust_and_validate_buy_quantity.assert_called_once_with(
            balance=balance_tracker.balance,
            order_quantity=grid_manager.get_order_size_for_grid_level.return_value,
            price=49000,
        )

    @pytest.mark.asyncio
    async def test_on_order_filled_unexpected_error(self, setup_order_manager):
        manager, _, _, _, order_book, _, _, _ = setup_order_manager
        mock_order = Mock()
        order_book.get_grid_level_for_order.return_value = Mock()
        manager._handle_order_completion = AsyncMock(side_effect=Exception("Unexpected error"))

        await manager._on_order_filled(mock_order)

        manager._handle_order_completion.assert_awaited_once()

    def test_get_or_create_paired_buy_level_no_fallback(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        mock_sell_grid_level = Mock(paired_buy_level=None)
        grid_manager.get_grid_level_below.return_value = None

        paired_buy_level = manager._get_or_create_paired_buy_level(mock_sell_grid_level)

        assert paired_buy_level is None
        grid_manager.get_grid_level_below.assert_called_once_with(mock_sell_grid_level)

    @pytest.mark.asyncio
    async def test_place_order_sell_failure(self, setup_order_manager):
        manager, grid_manager, order_validator, balance_tracker, order_book, _, order_execution_strategy, _ = (
            setup_order_manager
        )
        buy_grid_level = Mock(price=48000)
        sell_grid_level = Mock(price=52000)
        quantity = 0.01

        order_validator.adjust_and_validate_sell_quantity.return_value = quantity
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=None)

        await manager._place_order(OrderSide.SELL, buy_grid_level, sell_grid_level, quantity)

        grid_manager.pair_grid_levels.assert_not_called()
        balance_tracker.reserve_funds_for_sell.assert_not_called()
        grid_manager.mark_order_pending.assert_not_called()
        order_book.add_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_place_order_buy_failure(self, setup_order_manager):
        manager, grid_manager, order_validator, balance_tracker, order_book, _, order_execution_strategy, _ = (
            setup_order_manager
        )
        sell_grid_level = Mock(price=52000)
        buy_grid_level = Mock(price=48000)
        quantity = 0.01

        order_validator.adjust_and_validate_buy_quantity.return_value = quantity
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=None)

        await manager._place_order(OrderSide.BUY, sell_grid_level, buy_grid_level, quantity)

        grid_manager.pair_grid_levels.assert_not_called()
        balance_tracker.reserve_funds_for_buy.assert_not_called()
        grid_manager.mark_order_pending.assert_not_called()
        order_book.add_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_perform_initial_purchase_zero_quantity(self, setup_order_manager):
        manager, grid_manager, _, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.get_initial_order_quantity.return_value = 0
        balance_tracker.balance = 1000
        balance_tracker.crypto_balance = 0

        await manager.perform_initial_purchase(50000)

        order_execution_strategy.execute_market_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_take_profit_or_stop_loss_order_no_action(self, setup_order_manager):
        manager, _, _, _, _, _, order_execution_strategy, _ = setup_order_manager

        await manager.execute_take_profit_or_stop_loss_order(50000)

        order_execution_strategy.execute_market_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_take_profit_or_stop_loss_order_failure(self, setup_order_manager):
        manager, _, _, balance_tracker, _, _, order_execution_strategy, notification_handler = setup_order_manager
        balance_tracker.crypto_balance = 0.5

        # Mock the order execution to raise an error
        order_execution_strategy.execute_market_order = AsyncMock(
            side_effect=OrderExecutionFailedError(
                "Order execution failed",
                OrderSide.SELL,
                OrderType.MARKET,
                "BTC/USDT",
                0.5,
                55000,
            ),
        )
        notification_handler.async_send_notification = AsyncMock()

        await manager.execute_take_profit_or_stop_loss_order(55000, take_profit_order=True)

        notification_handler.async_send_notification.assert_awaited_with(
            NotificationType.ORDER_FAILED,
            error_details="Failed to place Take profit order: Order execution failed",
        )

    @pytest.mark.asyncio
    async def test_handle_sell_order_completion_no_paired_level(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        mock_order = Mock(side=OrderSide.SELL, filled=0.1)
        mock_grid_level = Mock(price=50000)
        manager._get_or_create_paired_buy_level = Mock(return_value=None)

        await manager._handle_sell_order_completion(mock_order, mock_grid_level)

        grid_manager.complete_order.assert_called_once_with(mock_grid_level, OrderSide.SELL)

    def test_get_or_create_paired_buy_level_with_valid_paired_level(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        mock_sell_grid_level = Mock()
        mock_paired_buy_level = Mock()
        mock_sell_grid_level.paired_buy_level = mock_paired_buy_level
        grid_manager.can_place_order.return_value = True

        result = manager._get_or_create_paired_buy_level(mock_sell_grid_level)

        assert result == mock_paired_buy_level
        grid_manager.get_grid_level_below.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_order_cancelled(self, setup_order_manager):
        manager, _, _, _, _, _, _, notification_handler = setup_order_manager
        mock_order = Mock()
        notification_handler.async_send_notification = AsyncMock()

        await manager._on_order_cancelled(mock_order)

        notification_handler.async_send_notification.assert_awaited_with(
            NotificationType.ORDER_CANCELLED,
            order_details=str(mock_order),
        )
