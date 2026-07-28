import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.grid_management.grid_level import GridCycleState, GridLevel
from grid_trading_bot.core.order_handling.exceptions import GridFeasibilityError, OrderExecutionFailedError
from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus, OrderType
from grid_trading_bot.core.services.exceptions import DataFetchError
from grid_trading_bot.core.services.market_constraints import MarketConstraints


def _mock_order(
    side=OrderSide.BUY,
    amount=1.0,
    filled=0.0,
    remaining=1.0,
    price=95.0,
    identifier="o1",
    status=None,
):
    return Mock(
        identifier=identifier,
        side=side,
        amount=amount,
        filled=filled,
        remaining=remaining,
        price=price,
        status=status,
    )


class TestOrderManager:
    # ── initialize_grid_orders ──────────────────────────────────────────

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

        await manager.initialize_grid_orders(50000)

        notification_handler.async_send_notification.assert_awaited_with(
            NotificationType.ORDER_FAILED,
            error_details="Error while placing initial buy order. Test error",
        )
        # Verify funds were released after failure
        balance_tracker.release_reserved_fiat.assert_awaited()

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_insufficient_balance(self, setup_order_manager):
        manager, grid_manager, order_validator, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.sorted_buy_grids = [49000]
        grid_manager.sorted_sell_grids = []
        grid_manager.grid_levels = {49000: Mock()}
        grid_manager.can_place_order.return_value = True
        order_validator.adjust_and_validate_buy_quantity.side_effect = ValueError("Insufficient balance")
        balance_tracker.balance = 0
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
    async def test_initialize_grid_orders_generic_exception_sends_error_notification(self, setup_order_manager):
        (
            manager,
            grid_manager,
            order_validator,
            balance_tracker,
            _,
            _,
            _order_execution_strategy,
            notification_handler,
        ) = setup_order_manager

        grid_manager.sorted_buy_grids = [48000]
        grid_manager.sorted_sell_grids = []
        grid_manager.grid_levels = {48000: Mock()}
        grid_manager.can_place_order.return_value = True
        order_validator.adjust_and_validate_buy_quantity.side_effect = RuntimeError("Something broke")
        balance_tracker.get_total_balance_value.return_value = 50000
        notification_handler.async_send_notification = AsyncMock()

        await manager.initialize_grid_orders(50000)

        notification_handler.async_send_notification.assert_awaited_with(
            NotificationType.ERROR_OCCURRED,
            error_details="Error while placing initial buy order: Something broke",
        )

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_none_result_releases_funds(self, setup_order_manager):
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

        grid_manager.sorted_buy_grids = [48000]
        grid_manager.sorted_sell_grids = []
        grid_manager.grid_levels = {48000: Mock()}
        grid_manager.can_place_order.return_value = True
        grid_manager.get_order_size_for_grid_level.return_value = 0.1
        order_validator.adjust_and_validate_buy_quantity.return_value = 0.1
        balance_tracker.get_total_balance_value.return_value = 50000
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=None)
        notification_handler.async_send_notification = AsyncMock()

        await manager.initialize_grid_orders(50000)

        # Funds should be reserved then released when order returns None
        balance_tracker.reserve_funds_for_buy.assert_awaited()
        balance_tracker.release_reserved_fiat.assert_awaited()

    # ── _on_order_filled ────────────────────────────────────────────────

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
    async def test_on_order_filled_ignored_after_positions_closed(self, setup_order_manager):
        manager, _, _, _, order_book, _, _, _ = setup_order_manager
        manager._positions_closed = True
        mock_order = Mock(side=OrderSide.BUY, price=50000, identifier="o1")

        await manager._on_order_filled(mock_order)

        order_book.get_grid_level_for_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_order_filled_no_grid_level(self, setup_order_manager):
        manager, _, _, _, order_book, _, _, _ = setup_order_manager
        mock_order = Mock()

        order_book.get_grid_level_for_order.return_value = None

        await manager._on_order_filled(mock_order)

        order_book.get_grid_level_for_order.assert_called_once_with(mock_order)

    @pytest.mark.asyncio
    async def test_on_order_filled_unexpected_error(self, setup_order_manager):
        manager, _, _, _, order_book, _, _, _ = setup_order_manager
        mock_order = Mock()
        order_book.get_grid_level_for_order.return_value = Mock()
        manager._handle_order_completion = AsyncMock(side_effect=DataFetchError("Unexpected error"))

        await manager._on_order_filled(mock_order)

        manager._handle_order_completion.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_on_order_filled_order_execution_failed_error(self, setup_order_manager):
        manager, _, _, _, order_book, _, _, notification_handler = setup_order_manager
        mock_order = Mock()
        order_book.get_grid_level_for_order.return_value = Mock()
        manager._handle_order_completion = AsyncMock(
            side_effect=OrderExecutionFailedError(
                "Execution failed",
                OrderSide.BUY,
                OrderType.LIMIT,
                "BTC/USD",
                0.01,
                50000,
            ),
        )
        notification_handler.async_send_notification = AsyncMock()

        await manager._on_order_filled(mock_order)

        notification_handler.async_send_notification.assert_awaited_with(
            NotificationType.ORDER_FAILED,
            error_details="Failed handling filled order. Execution failed",
        )

    @pytest.mark.asyncio
    async def test_on_order_filled_concurrent_calls_serialized(self, setup_order_manager):
        """Two concurrent _on_order_filled calls are serialized by the lock."""
        manager, _, _, _, order_book, _, _, _ = setup_order_manager
        call_order = []

        async def mock_handle(order, grid_level):
            call_order.append(f"start_{order.identifier}")
            await asyncio.sleep(0.05)
            call_order.append(f"end_{order.identifier}")

        manager._handle_order_completion = mock_handle

        order1 = Mock(side=OrderSide.BUY, price=50000, identifier="order1")
        order2 = Mock(side=OrderSide.SELL, price=51000, identifier="order2")
        order_book.get_grid_level_for_order.return_value = Mock()

        await asyncio.gather(
            manager._on_order_filled(order1),
            manager._on_order_filled(order2),
        )

        # With the lock, calls should be serialized: start_X, end_X, start_Y, end_Y
        assert call_order[0].startswith("start_")
        assert call_order[1].startswith("end_")
        assert call_order[2].startswith("start_")
        assert call_order[3].startswith("end_")

    # ── _handle_order_completion ────────────────────────────────────────

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
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        mock_order = Mock(side=OrderSide.SELL, filled=0.01)
        mock_grid_level = Mock(price=50000)
        grid_manager.get_or_create_paired_buy_level.return_value = Mock(price=48000)
        manager._place_order = AsyncMock()

        await manager._handle_order_completion(mock_order, mock_grid_level)

        manager._place_order.assert_awaited_once()

    # ── _handle_buy_order_completion ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_handle_buy_order_completion_no_paired_sell_level(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        mock_order = Mock(side=OrderSide.BUY, filled=0.01)
        mock_grid_level = Mock(price=50000)
        grid_manager.get_paired_sell_level.return_value = None
        manager._place_order = AsyncMock()

        await manager._handle_buy_order_completion(mock_order, mock_grid_level)

        grid_manager.complete_order.assert_called_once_with(mock_grid_level, OrderSide.BUY)
        manager._place_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_buy_order_completion_sell_level_cannot_place(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        mock_order = Mock(side=OrderSide.BUY, filled=0.01)
        mock_grid_level = Mock(price=50000)
        grid_manager.get_paired_sell_level.return_value = Mock()
        grid_manager.can_place_order.return_value = False
        manager._place_order = AsyncMock()

        await manager._handle_buy_order_completion(mock_order, mock_grid_level)

        grid_manager.complete_order.assert_called_once_with(mock_grid_level, OrderSide.BUY)
        manager._place_order.assert_not_awaited()

    # ── _handle_sell_order_completion ───────────────────────────────────

    @pytest.mark.asyncio
    async def test_handle_sell_order_completion_no_paired_level(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        mock_order = Mock(side=OrderSide.SELL, filled=0.1)
        mock_grid_level = Mock(price=50000)
        grid_manager.get_or_create_paired_buy_level.return_value = None

        await manager._handle_sell_order_completion(mock_order, mock_grid_level)

        grid_manager.complete_order.assert_called_once_with(mock_grid_level, OrderSide.SELL)

    # ── _place_order ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_place_order_sell_success(self, setup_order_manager):
        (
            manager,
            grid_manager,
            order_validator,
            balance_tracker,
            order_book,
            _,
            order_execution_strategy,
            notification_handler,
        ) = setup_order_manager
        source_grid_level = Mock(price=48000)
        target_grid_level = Mock(price=52000)
        quantity = 0.01
        mock_order = Mock(amount=quantity)

        order_validator.adjust_and_validate_sell_quantity.return_value = quantity
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=mock_order)
        notification_handler.async_send_notification = AsyncMock()

        await manager._place_order(OrderSide.SELL, source_grid_level, target_grid_level, quantity)

        # Reserve is called before placing the order
        balance_tracker.reserve_funds_for_sell.assert_awaited_once_with(quantity)
        grid_manager.pair_grid_levels.assert_called_once_with(
            source_grid_level,
            target_grid_level,
            pairing_type="sell",
        )
        grid_manager.mark_order_pending.assert_called_once_with(target_grid_level, mock_order)
        order_book.add_order.assert_called_once_with(mock_order, target_grid_level)
        notification_handler.async_send_notification.assert_awaited_once_with(
            NotificationType.ORDER_PLACED,
            order_details=str(mock_order),
        )

    @pytest.mark.asyncio
    async def test_place_order_buy_success(self, setup_order_manager):
        (
            manager,
            grid_manager,
            order_validator,
            balance_tracker,
            order_book,
            _,
            order_execution_strategy,
            notification_handler,
        ) = setup_order_manager
        source_grid_level = Mock(price=52000)
        target_grid_level = Mock(price=48000)
        quantity = 0.01
        mock_order = Mock(amount=quantity)

        order_validator.adjust_and_validate_buy_quantity.return_value = quantity
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=mock_order)
        notification_handler.async_send_notification = AsyncMock()

        await manager._place_order(OrderSide.BUY, source_grid_level, target_grid_level, quantity)

        # Reserve is called before placing the order
        balance_tracker.reserve_funds_for_buy.assert_awaited_once_with(quantity * 48000)
        grid_manager.pair_grid_levels.assert_called_once_with(
            source_grid_level,
            target_grid_level,
            pairing_type="buy",
        )
        grid_manager.mark_order_pending.assert_called_once_with(target_grid_level, mock_order)
        order_book.add_order.assert_called_once_with(mock_order, target_grid_level)

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

        # Reserve is called first, then released on failure
        balance_tracker.reserve_funds_for_sell.assert_awaited_once_with(quantity)
        balance_tracker.release_reserved_crypto.assert_awaited_once_with(quantity)
        grid_manager.pair_grid_levels.assert_not_called()
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

        # Reserve is called first, then released on failure
        balance_tracker.reserve_funds_for_buy.assert_awaited_once_with(quantity * 48000)
        balance_tracker.release_reserved_fiat.assert_awaited_once_with(quantity * 48000)
        grid_manager.pair_grid_levels.assert_not_called()
        grid_manager.mark_order_pending.assert_not_called()
        order_book.add_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_place_order_exception_releases_funds(self, setup_order_manager):
        manager, _, order_validator, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        source_grid_level = Mock(price=48000)
        target_grid_level = Mock(price=52000)
        quantity = 0.01

        order_validator.adjust_and_validate_sell_quantity.return_value = quantity
        order_execution_strategy.execute_limit_order = AsyncMock(
            side_effect=OrderExecutionFailedError(
                "Exchange error", OrderSide.SELL, OrderType.LIMIT, "BTC/USDT", quantity, 52000
            ),
        )

        with pytest.raises(OrderExecutionFailedError):
            await manager._place_order(OrderSide.SELL, source_grid_level, target_grid_level, quantity)

        balance_tracker.reserve_funds_for_sell.assert_awaited_once_with(quantity)
        balance_tracker.release_reserved_crypto.assert_awaited_once_with(quantity)

    # ── perform_initial_purchase ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_perform_initial_purchase(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.get_initial_order_quantity.return_value = 0.01
        order_execution_strategy.execute_market_order = AsyncMock(return_value=Mock())

        await manager.perform_initial_purchase(50000)

        order_execution_strategy.execute_market_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_perform_initial_purchase_zero_quantity(self, setup_order_manager):
        manager, grid_manager, _, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.get_initial_order_quantity.return_value = 0
        balance_tracker.balance = 1000
        balance_tracker.crypto_balance = 0

        await manager.perform_initial_purchase(50000)

        order_execution_strategy.execute_market_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_perform_initial_purchase_backtest_calls_simulate_fill(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, order_execution_strategy, _ = setup_order_manager
        manager.trading_mode = TradingMode.BACKTEST
        mock_order = Mock(timestamp=1234567890)
        grid_manager.get_initial_order_quantity.return_value = 0.01
        order_execution_strategy.execute_market_order = AsyncMock(return_value=mock_order)

        await manager.perform_initial_purchase(50000)

        manager.order_simulator.simulate_fill.assert_awaited_once_with(mock_order, mock_order.timestamp)

    @pytest.mark.asyncio
    async def test_perform_initial_purchase_live_calls_update_balance(self, setup_order_manager):
        manager, grid_manager, _, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        mock_order = Mock(timestamp=1234567890)
        grid_manager.get_initial_order_quantity.return_value = 0.01
        order_execution_strategy.execute_market_order = AsyncMock(return_value=mock_order)
        balance_tracker.update_after_initial_purchase = AsyncMock()

        await manager.perform_initial_purchase(50000)

        balance_tracker.update_after_initial_purchase.assert_awaited_once_with(initial_order=mock_order)
        manager.order_simulator.simulate_fill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_perform_initial_purchase_execution_failed(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, order_execution_strategy, notification_handler = setup_order_manager
        grid_manager.get_initial_order_quantity.return_value = 0.01
        order_execution_strategy.execute_market_order = AsyncMock(
            side_effect=OrderExecutionFailedError(
                "Purchase failed",
                OrderSide.BUY,
                OrderType.MARKET,
                "BTC/USD",
                0.01,
                50000,
            ),
        )
        notification_handler.async_send_notification = AsyncMock()

        await manager.perform_initial_purchase(50000)

        notification_handler.async_send_notification.assert_awaited_with(
            NotificationType.ORDER_FAILED,
            error_details="Error while performing initial purchase. Purchase failed",
        )

    @pytest.mark.asyncio
    async def test_perform_initial_purchase_generic_exception(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, order_execution_strategy, notification_handler = setup_order_manager
        grid_manager.get_initial_order_quantity.return_value = 0.01
        order_execution_strategy.execute_market_order = AsyncMock(
            side_effect=DataFetchError("Network error"),
        )
        notification_handler.async_send_notification = AsyncMock()

        await manager.perform_initial_purchase(50000)

        notification_handler.async_send_notification.assert_awaited_with(
            NotificationType.ORDER_FAILED,
            error_details="Error while performing initial purchase. Network error",
        )

    # ── execute_take_profit_or_stop_loss_order ──────────────────────────

    @pytest.mark.asyncio
    async def test_execute_take_profit_or_stop_loss_order(self, setup_order_manager):
        manager, _, _, balance_tracker, _, _, order_execution_strategy, notification_handler = setup_order_manager
        manager.trading_mode = TradingMode.BACKTEST
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
    async def test_execute_stop_loss_order(self, setup_order_manager):
        manager, _, _, balance_tracker, _, _, order_execution_strategy, notification_handler = setup_order_manager
        manager.trading_mode = TradingMode.BACKTEST
        balance_tracker.crypto_balance = 0.3
        order_execution_strategy.execute_market_order = AsyncMock(return_value=Mock())
        notification_handler.async_send_notification = AsyncMock()

        await manager.execute_take_profit_or_stop_loss_order(40000, stop_loss_order=True)

        order_execution_strategy.execute_market_order.assert_awaited_once_with(
            OrderSide.SELL,
            manager.trading_pair,
            0.3,
            40000,
        )
        notification_handler.async_send_notification.assert_awaited_with(
            NotificationType.STOP_LOSS_TRIGGERED,
            order_details=str(order_execution_strategy.execute_market_order.return_value),
        )

    @pytest.mark.asyncio
    async def test_execute_take_profit_or_stop_loss_order_no_action(self, setup_order_manager):
        manager, _, _, _, _, _, order_execution_strategy, _ = setup_order_manager

        await manager.execute_take_profit_or_stop_loss_order(50000)

        order_execution_strategy.execute_market_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_take_profit_or_stop_loss_order_failure(self, setup_order_manager):
        manager, _, _, balance_tracker, _, _, order_execution_strategy, notification_handler = setup_order_manager
        manager.trading_mode = TradingMode.BACKTEST
        balance_tracker.crypto_balance = 0.5

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
    async def test_execute_take_profit_or_stop_loss_order_generic_exception(self, setup_order_manager):
        manager, _, _, balance_tracker, _, _, order_execution_strategy, notification_handler = setup_order_manager
        manager.trading_mode = TradingMode.BACKTEST
        balance_tracker.crypto_balance = 0.5

        order_execution_strategy.execute_market_order = AsyncMock(
            side_effect=DataFetchError("Connection lost"),
        )
        notification_handler.async_send_notification = AsyncMock()

        await manager.execute_take_profit_or_stop_loss_order(55000, take_profit_order=True)

        notification_handler.async_send_notification.assert_awaited_with(
            NotificationType.ERROR_OCCURRED,
            error_details="Failed to place Take profit order: Connection lost",
        )

    # ── Buy/Sell Ratio ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_buy_fill_places_sell_with_sell_ratio(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        grid_manager.sell_ratio = 0.5
        mock_order = Mock(side=OrderSide.BUY, filled=1.0)
        mock_grid_level = Mock(price=50000)
        paired_sell_level = Mock(price=52000)
        grid_manager.get_paired_sell_level.return_value = paired_sell_level
        grid_manager.can_place_order.return_value = True
        manager._place_order = AsyncMock()

        await manager._handle_buy_order_completion(mock_order, mock_grid_level)

        manager._place_order.assert_awaited_once_with(OrderSide.SELL, mock_grid_level, paired_sell_level, 0.5)

    @pytest.mark.asyncio
    async def test_buy_fill_places_sell_with_default_ratio(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        grid_manager.sell_ratio = 1.0
        mock_order = Mock(side=OrderSide.BUY, filled=1.0)
        mock_grid_level = Mock(price=50000)
        paired_sell_level = Mock(price=52000)
        grid_manager.get_paired_sell_level.return_value = paired_sell_level
        grid_manager.can_place_order.return_value = True
        manager._place_order = AsyncMock()

        await manager._handle_buy_order_completion(mock_order, mock_grid_level)

        manager._place_order.assert_awaited_once_with(OrderSide.SELL, mock_grid_level, paired_sell_level, 1.0)

    @pytest.mark.asyncio
    async def test_sell_fill_places_buy_with_default_ratio(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        grid_manager.buy_ratio = 1.0
        mock_order = Mock(side=OrderSide.SELL, filled=0.5)
        mock_grid_level = Mock(price=52000)
        paired_buy_level = Mock(price=50000)
        grid_manager.get_or_create_paired_buy_level.return_value = paired_buy_level
        manager._place_order = AsyncMock()

        await manager._handle_sell_order_completion(mock_order, mock_grid_level)

        # buy_qty = 0.5 * 1.0 = 0.5 (same as filled, backward compatible)
        manager._place_order.assert_awaited_once_with(OrderSide.BUY, mock_grid_level, paired_buy_level, 0.5)

    @pytest.mark.asyncio
    async def test_sell_fill_places_buy_with_buy_ratio_half(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        grid_manager.buy_ratio = 0.5
        mock_order = Mock(side=OrderSide.SELL, filled=1.0)
        mock_grid_level = Mock(price=52000)
        paired_buy_level = Mock(price=50000)
        grid_manager.get_or_create_paired_buy_level.return_value = paired_buy_level
        manager._place_order = AsyncMock()

        await manager._handle_sell_order_completion(mock_order, mock_grid_level)

        # buy_qty = 1.0 * 0.5 = 0.5 (accumulate 50% fiat)
        manager._place_order.assert_awaited_once_with(OrderSide.BUY, mock_grid_level, paired_buy_level, 0.5)


class TestCancelOpenGridOrders:
    # ── cancel_open_grid_orders ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_cancel_settles_remaining_not_amount(self, setup_order_manager):
        # trading_mode defaults to LIVE on setup_order_manager
        manager, grid_manager, _, balance_tracker, order_book, _, order_execution_strategy, _ = setup_order_manager
        order = Mock(identifier="b1", side=OrderSide.BUY, amount=1.0, filled=0.4, remaining=0.6, price=95.0)
        refetched = Mock(identifier="b1", side=OrderSide.BUY, amount=1.0, filled=0.4, remaining=0.6, price=95.0)
        grid_level = Mock(price=95.0)
        order_book.get_buy_orders_with_grid.return_value = [(order, grid_level)]
        order_book.get_sell_orders_with_grid.return_value = []
        order_book.get_open_orders.return_value = [order]
        order_execution_strategy.cancel_order = AsyncMock(return_value=True)
        order_execution_strategy.get_order = AsyncMock(return_value=refetched)

        result = await manager.cancel_open_grid_orders()

        assert result is True
        order_execution_strategy.get_order.assert_awaited_once_with("b1", manager.trading_pair)
        # settles the refetched (authoritative) order, not the locally-cached one
        balance_tracker.settle_cancelled_order.assert_awaited_once_with(refetched)
        grid_manager.rollback_order_placement.assert_called_once_with(grid_level, OrderSide.BUY)
        order_book.remove_open_order.assert_called_once_with(order)

    @pytest.mark.asyncio
    async def test_cancel_refetch_failure_falls_back_to_local_order(self, setup_order_manager):
        manager, grid_manager, _, balance_tracker, order_book, _, order_execution_strategy, _ = setup_order_manager
        order = Mock(identifier="b1", side=OrderSide.BUY, amount=1.0, filled=0.4, remaining=0.6, price=95.0)
        grid_level = Mock(price=95.0)
        order_book.get_buy_orders_with_grid.return_value = [(order, grid_level)]
        order_book.get_sell_orders_with_grid.return_value = []
        order_book.get_open_orders.return_value = [order]
        order_execution_strategy.cancel_order = AsyncMock(return_value=True)
        order_execution_strategy.get_order = AsyncMock(side_effect=DataFetchError("boom"))

        result = await manager.cancel_open_grid_orders()

        assert result is True
        balance_tracker.settle_cancelled_order.assert_awaited_once_with(order)
        grid_manager.rollback_order_placement.assert_called_once_with(grid_level, OrderSide.BUY)

    @pytest.mark.asyncio
    async def test_cancel_backtest_skips_refetch(self, setup_order_manager):
        manager, grid_manager, _, balance_tracker, order_book, _, order_execution_strategy, _ = setup_order_manager
        manager.trading_mode = TradingMode.BACKTEST
        order = Mock(identifier="b1", side=OrderSide.BUY, amount=1.0, filled=0.0, remaining=1.0, price=95.0)
        grid_level = Mock(price=95.0)
        order_book.get_buy_orders_with_grid.return_value = [(order, grid_level)]
        order_book.get_sell_orders_with_grid.return_value = []
        order_book.get_open_orders.return_value = [order]
        order_execution_strategy.cancel_order = AsyncMock(return_value=True)
        order_execution_strategy.get_order = AsyncMock()

        result = await manager.cancel_open_grid_orders()

        assert result is True
        order_execution_strategy.get_order.assert_not_awaited()
        balance_tracker.settle_cancelled_order.assert_awaited_once_with(order)
        grid_manager.rollback_order_placement.assert_called_once_with(grid_level, OrderSide.BUY)

    @pytest.mark.asyncio
    async def test_cancels_all_open_orders_sell_side(self, setup_order_manager):
        manager, grid_manager, _, balance_tracker, order_book, _, order_execution_strategy, _ = setup_order_manager
        sell_order = Mock(identifier="s1", side=OrderSide.SELL, amount=1.0, filled=0.0, remaining=1.0, price=105.0)
        sell_level = Mock(price=105.0)
        order_book.get_buy_orders_with_grid.return_value = []
        order_book.get_sell_orders_with_grid.return_value = [(sell_order, sell_level)]
        order_book.get_open_orders.return_value = [sell_order]
        order_execution_strategy.cancel_order = AsyncMock(return_value=True)
        order_execution_strategy.get_order = AsyncMock(return_value=sell_order)

        result = await manager.cancel_open_grid_orders()

        assert result is True
        order_book.remove_open_order.assert_called_once_with(sell_order)
        balance_tracker.settle_cancelled_order.assert_awaited_once_with(sell_order)
        grid_manager.rollback_order_placement.assert_called_once_with(sell_level, OrderSide.SELL)

    @pytest.mark.asyncio
    async def test_partial_failure_returns_false_and_stops(self, setup_order_manager):
        manager, grid_manager, _, balance_tracker, order_book, _, order_execution_strategy, _ = setup_order_manager
        o1 = Mock(identifier="b1", side=OrderSide.BUY, amount=1.0, filled=0.0, remaining=1.0, price=95.0)
        o2 = Mock(identifier="b2", side=OrderSide.BUY, amount=1.0, filled=0.0, remaining=1.0, price=90.0)
        lvl1, lvl2 = Mock(price=95.0), Mock(price=90.0)
        order_book.get_buy_orders_with_grid.return_value = [(o1, lvl1), (o2, lvl2)]
        order_book.get_sell_orders_with_grid.return_value = []
        order_book.get_open_orders.return_value = [o1, o2]
        order_execution_strategy.cancel_order = AsyncMock(side_effect=[True, False])
        order_execution_strategy.get_order = AsyncMock(return_value=o1)

        result = await manager.cancel_open_grid_orders()

        assert result is False
        order_book.remove_open_order.assert_called_once_with(o1)
        # only o1's settlement happened; o2's cancel failed before any settlement was attempted
        balance_tracker.settle_cancelled_order.assert_awaited_once_with(o1)
        grid_manager.rollback_order_placement.assert_called_once_with(lvl1, OrderSide.BUY)

    @pytest.mark.asyncio
    async def test_no_open_orders_is_success(self, setup_order_manager):
        manager, _, _, _, order_book, _, _, _ = setup_order_manager
        order_book.get_buy_orders_with_grid.return_value = []
        order_book.get_sell_orders_with_grid.return_value = []
        order_book.get_open_orders.return_value = []

        assert await manager.cancel_open_grid_orders() is True


class TestMarketConstraintsEnforcement:
    # ── validate_grid_feasibility ────────────────────────────────────────

    def test_validate_grid_feasibility_raises_on_dust_level(self, setup_order_manager):
        manager, grid_manager, _, balance_tracker, _, _, _, _ = setup_order_manager
        manager.set_market_constraints(MarketConstraints(min_amount=None, min_cost=50.0))
        grid_manager.sorted_buy_grids = [90.0, 95.0]
        grid_manager.sorted_sell_grids = [105.0, 110.0]
        balance_tracker.get_total_balance_value.return_value = 100.0
        grid_manager.get_order_size_for_grid_level.return_value = 0.1  # notional ~10 < 50

        with pytest.raises(GridFeasibilityError, match="minimum cost"):
            manager.validate_grid_feasibility(current_price=100.0)

    def test_validate_grid_feasibility_noop_without_constraints(self, setup_order_manager):
        manager, *_ = setup_order_manager

        manager.validate_grid_feasibility(current_price=100.0)  # must not raise

    def test_validate_grid_feasibility_passes_when_above_minimums(self, setup_order_manager):
        manager, grid_manager, _, balance_tracker, _, _, _, _ = setup_order_manager
        manager.set_market_constraints(MarketConstraints(min_amount=None, min_cost=50.0))
        grid_manager.sorted_buy_grids = [90.0, 95.0]
        grid_manager.sorted_sell_grids = [105.0, 110.0]
        balance_tracker.get_total_balance_value.return_value = 100000.0
        grid_manager.get_order_size_for_grid_level.return_value = 1.0  # notional ~100 > 50

        manager.validate_grid_feasibility(current_price=100.0)  # must not raise

    # ── _place_order dust guard ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_place_order_skips_below_minimum(self, setup_order_manager):
        (
            manager,
            grid_manager,
            order_validator,
            balance_tracker,
            order_book,
            _,
            order_execution_strategy,
            _,
        ) = setup_order_manager
        manager.set_market_constraints(MarketConstraints(min_amount=1.0, min_cost=None))
        source_grid_level = Mock(price=48000)
        target_grid_level = Mock(price=52000)
        order_validator.adjust_and_validate_buy_quantity.return_value = 0.5
        order_execution_strategy.execute_limit_order = AsyncMock()

        await manager._place_order(OrderSide.BUY, source_grid_level, target_grid_level, 0.5)

        order_execution_strategy.execute_limit_order.assert_not_awaited()
        balance_tracker.reserve_funds_for_buy.assert_not_awaited()
        grid_manager.pair_grid_levels.assert_not_called()
        order_book.add_order.assert_not_called()

    # ── perform_initial_purchase dust guard ──────────────────────────────

    @pytest.mark.asyncio
    async def test_initial_purchase_skipped_when_dust(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, order_execution_strategy, _ = setup_order_manager
        manager.set_market_constraints(MarketConstraints(min_amount=None, min_cost=1000.0))
        grid_manager.get_initial_order_quantity.return_value = 0.1

        await manager.perform_initial_purchase(current_price=100.0)

        order_execution_strategy.execute_market_order.assert_not_called()

    # ── _initialize_orders defensive skip ────────────────────────────────

    @pytest.mark.asyncio
    async def test_initialize_orders_skips_dust_level(self, setup_order_manager):
        manager, grid_manager, order_validator, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        manager.set_market_constraints(MarketConstraints(min_amount=1.0, min_cost=None))
        grid_manager.sorted_buy_grids = [48000]
        grid_manager.sorted_sell_grids = []
        grid_manager.grid_levels = {48000: Mock()}
        grid_manager.can_place_order.return_value = True
        grid_manager.get_order_size_for_grid_level.return_value = 0.1
        order_validator.adjust_and_validate_buy_quantity.return_value = 0.1
        balance_tracker.get_total_balance_value.return_value = 50000
        order_execution_strategy.execute_limit_order = AsyncMock()

        await manager.initialize_grid_orders(50000)

        order_execution_strategy.execute_limit_order.assert_not_awaited()


class TestOnOrderCancelled:
    # ── _on_order_cancelled ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_settles_rolls_back_and_replaces(self, setup_order_manager):
        (
            manager,
            grid_manager,
            order_validator,
            balance_tracker,
            order_book,
            _,
            order_execution_strategy,
            _,
        ) = setup_order_manager
        grid_level = GridLevel(price=95.0, state=GridCycleState.WAITING_FOR_BUY_FILL)
        order = _mock_order(side=OrderSide.BUY, amount=1.0, filled=0.0, remaining=1.0, price=95.0)
        order_book.get_grid_level_for_order.return_value = grid_level
        order_validator.adjust_and_validate_buy_quantity.return_value = 1.0
        new_order = _mock_order(side=OrderSide.BUY, amount=1.0, filled=0.0, remaining=1.0, price=95.0)
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=new_order)

        await manager._on_order_cancelled(order)

        balance_tracker.settle_cancelled_order.assert_awaited_once_with(order)
        grid_manager.rollback_order_placement.assert_called_once_with(grid_level, OrderSide.BUY)
        grid_manager.mark_order_pending.assert_called_once_with(grid_level, new_order)
        order_book.add_order.assert_called_once_with(new_order, grid_level)

    @pytest.mark.asyncio
    async def test_settles_rolls_back_and_replaces_sell_side(self, setup_order_manager):
        (
            manager,
            grid_manager,
            order_validator,
            balance_tracker,
            order_book,
            _,
            order_execution_strategy,
            _,
        ) = setup_order_manager
        grid_level = GridLevel(price=105.0, state=GridCycleState.WAITING_FOR_SELL_FILL)
        order = _mock_order(side=OrderSide.SELL, amount=1.0, filled=0.0, remaining=1.0, price=105.0)
        order_book.get_grid_level_for_order.return_value = grid_level
        order_validator.adjust_and_validate_sell_quantity.return_value = 1.0
        new_order = _mock_order(side=OrderSide.SELL, amount=1.0, filled=0.0, remaining=1.0, price=105.0)
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=new_order)

        await manager._on_order_cancelled(order)

        balance_tracker.settle_cancelled_order.assert_awaited_once_with(order)
        grid_manager.rollback_order_placement.assert_called_once_with(grid_level, OrderSide.SELL)
        grid_manager.mark_order_pending.assert_called_once_with(grid_level, new_order)
        order_book.add_order.assert_called_once_with(new_order, grid_level)

    @pytest.mark.asyncio
    async def test_skips_when_level_already_rolled_back(self, setup_order_manager):
        manager, _, _, balance_tracker, order_book, _, order_execution_strategy, _ = setup_order_manager
        # bulk cancel already handled it (level no longer WAITING_FOR_*)
        grid_level = GridLevel(price=95.0, state=GridCycleState.READY_TO_BUY)
        order = _mock_order(side=OrderSide.BUY)
        order_book.get_grid_level_for_order.return_value = grid_level
        order_execution_strategy.execute_limit_order = AsyncMock()

        await manager._on_order_cancelled(order)

        balance_tracker.settle_cancelled_order.assert_not_awaited()
        order_execution_strategy.execute_limit_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_grid_level_notifies_only(self, setup_order_manager):
        manager, _, _, balance_tracker, order_book, _, _, notification_handler = setup_order_manager
        order = _mock_order(side=OrderSide.BUY)
        order_book.get_grid_level_for_order.return_value = None

        await manager._on_order_cancelled(order)

        balance_tracker.settle_cancelled_order.assert_not_awaited()
        notification_handler.async_send_notification.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_replacement_failure_releases_reservation(self, setup_order_manager):
        (
            manager,
            _,
            order_validator,
            balance_tracker,
            order_book,
            _,
            order_execution_strategy,
            _,
        ) = setup_order_manager
        grid_level = GridLevel(price=95.0, state=GridCycleState.WAITING_FOR_BUY_FILL)
        order = _mock_order(side=OrderSide.BUY)
        order_book.get_grid_level_for_order.return_value = grid_level
        order_validator.adjust_and_validate_buy_quantity.return_value = 1.0
        order_execution_strategy.execute_limit_order = AsyncMock(side_effect=OrderExecutionFailedError("boom"))

        await manager._on_order_cancelled(order)  # must not raise

        balance_tracker.release_reserved_fiat.assert_awaited()


class TestClosePositions:
    # ── close_positions ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_cancel_only_keeps_position(self, setup_order_manager):
        manager, _, _, _, _, _, order_execution_strategy, _ = setup_order_manager
        manager.cancel_open_grid_orders = AsyncMock(return_value=True)
        order_execution_strategy.execute_market_order = AsyncMock()

        await manager.close_positions(liquidate=False)

        manager.cancel_open_grid_orders.assert_awaited_once()
        order_execution_strategy.execute_market_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_liquidate_sells_full_crypto_balance_and_updates_tracker(self, setup_order_manager):
        manager, _, _, balance_tracker, order_book, _, order_execution_strategy, _ = setup_order_manager
        manager.cancel_open_grid_orders = AsyncMock(return_value=True)
        balance_tracker.crypto_balance = 2.5  # includes released reservations
        closed_order = _mock_order(side=OrderSide.SELL, status=OrderStatus.CLOSED, filled=2.5, amount=2.5)
        order_execution_strategy.execute_market_order = AsyncMock(return_value=closed_order)

        await manager.close_positions(liquidate=True, current_price=100.0)

        order_execution_strategy.execute_market_order.assert_awaited_once_with(
            OrderSide.SELL,
            manager.trading_pair,
            2.5,
            100.0,
        )
        balance_tracker.update_after_liquidation.assert_awaited_once_with(closed_order)
        order_book.add_order.assert_called_once_with(closed_order)

    @pytest.mark.asyncio
    async def test_second_call_is_noop(self, setup_order_manager):
        manager, _, _, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        manager.cancel_open_grid_orders = AsyncMock(return_value=True)
        balance_tracker.crypto_balance = 1.0
        closed_order = _mock_order(side=OrderSide.SELL, status=OrderStatus.CLOSED, filled=1.0, amount=1.0)
        order_execution_strategy.execute_market_order = AsyncMock(return_value=closed_order)

        await manager.close_positions(liquidate=True, current_price=100.0)
        await manager.close_positions(liquidate=False)

        manager.cancel_open_grid_orders.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_shutdown_state_rearms(self, setup_order_manager):
        manager, *_ = setup_order_manager
        manager.cancel_open_grid_orders = AsyncMock(return_value=True)

        await manager.close_positions(liquidate=False)
        manager.reset_shutdown_state()
        await manager.close_positions(liquidate=False)

        assert manager.cancel_open_grid_orders.await_count == 2

    @pytest.mark.asyncio
    async def test_cancel_failure_still_attempts_liquidation_and_notifies(self, setup_order_manager):
        manager, _, _, balance_tracker, _, _, order_execution_strategy, notification_handler = setup_order_manager
        manager.cancel_open_grid_orders = AsyncMock(return_value=False)
        balance_tracker.crypto_balance = 1.0
        closed_order = _mock_order(side=OrderSide.SELL, status=OrderStatus.CLOSED, filled=1.0, amount=1.0)
        order_execution_strategy.execute_market_order = AsyncMock(return_value=closed_order)

        await manager.close_positions(liquidate=True, current_price=100.0)

        order_execution_strategy.execute_market_order.assert_awaited_once()
        notification_handler.async_send_notification.assert_awaited()  # cancel-failure alert

    @pytest.mark.asyncio
    async def test_dust_liquidation_skipped(self, setup_order_manager):
        manager, _, _, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        manager.cancel_open_grid_orders = AsyncMock(return_value=True)
        manager.set_market_constraints(MarketConstraints(min_amount=None, min_cost=1000.0))
        balance_tracker.crypto_balance = 0.001
        order_execution_strategy.execute_market_order = AsyncMock()

        await manager.close_positions(liquidate=True, current_price=100.0)

        order_execution_strategy.execute_market_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_crypto_balance_skips_liquidation(self, setup_order_manager):
        manager, _, _, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        manager.cancel_open_grid_orders = AsyncMock(return_value=True)
        balance_tracker.crypto_balance = 0.0
        order_execution_strategy.execute_market_order = AsyncMock()

        await manager.close_positions(liquidate=True, current_price=100.0)

        order_execution_strategy.execute_market_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_deadlock_with_real_cancel_open_grid_orders(self, setup_order_manager):
        # Regression test: cancel_open_grid_orders() acquires OrderManager._lock itself,
        # so close_positions must call it *before* taking the lock for liquidation.
        manager, _, _, balance_tracker, order_book, _, order_execution_strategy, _ = setup_order_manager
        order_book.get_open_orders.return_value = []
        order_book.get_buy_orders_with_grid.return_value = []
        order_book.get_sell_orders_with_grid.return_value = []
        balance_tracker.crypto_balance = 1.0
        closed_order = _mock_order(side=OrderSide.SELL, status=OrderStatus.CLOSED, filled=1.0, amount=1.0)
        order_execution_strategy.execute_market_order = AsyncMock(return_value=closed_order)

        await asyncio.wait_for(manager.close_positions(liquidate=True, current_price=100.0), timeout=2)


class TestTakeProfitStopLossLive:
    # ── execute_take_profit_or_stop_loss_order (LIVE/PAPER_TRADING) ─────

    @pytest.mark.asyncio
    async def test_tp_delegates_to_close_positions(self, setup_order_manager):
        manager, *_ = setup_order_manager
        manager.close_positions = AsyncMock()

        await manager.execute_take_profit_or_stop_loss_order(current_price=100.0, take_profit_order=True)

        manager.close_positions.assert_awaited_once_with(liquidate=True, current_price=100.0)

    @pytest.mark.asyncio
    async def test_sl_delegates_to_close_positions_and_notifies(self, setup_order_manager):
        manager, _, _, _, _, _, _, notification_handler = setup_order_manager
        manager.close_positions = AsyncMock()

        await manager.execute_take_profit_or_stop_loss_order(current_price=90.0, stop_loss_order=True)

        manager.close_positions.assert_awaited_once_with(liquidate=True, current_price=90.0)
        notification_handler.async_send_notification.assert_awaited_once_with(
            NotificationType.STOP_LOSS_TRIGGERED,
            order_details="Stop loss at 90.0: open orders cancelled, position liquidated.",
        )
