import asyncio
from unittest.mock import AsyncMock, Mock, call

import pytest

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.grid_management.grid_level import GridCycleState, GridLevel
from grid_trading_bot.core.order_handling.exceptions import GridFeasibilityError, OrderExecutionFailedError
from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus, OrderType
from grid_trading_bot.core.order_handling.state_store import ArmingFlags
from grid_trading_bot.core.services.exceptions import DataFetchError, PostOnlyRejectedError
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
        grid_manager.get_initial_order_plan.return_value = [
            (Mock(price=49000), OrderSide.BUY),
            (Mock(price=48000), OrderSide.BUY),
        ]
        grid_manager.can_place_order.return_value = True
        order_validator.adjust_and_validate_buy_quantity.return_value = 0.01
        balance_tracker.balance = 1000
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=Mock())

        await manager.initialize_grid_orders(49500)

        grid_manager.can_place_order.assert_called()
        grid_manager.get_initial_order_plan.assert_called_once_with(49500)
        assert order_execution_strategy.execute_limit_order.call_count == 2

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_sell_orders(self, setup_order_manager):
        manager, grid_manager, order_validator, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.get_initial_order_plan.return_value = [
            (Mock(price=52000), OrderSide.SELL),
            (Mock(price=53000), OrderSide.SELL),
            (Mock(price=54000), OrderSide.SELL),
        ]
        grid_manager.can_place_order.return_value = True
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

        grid_manager.get_initial_order_plan.return_value = [(Mock(price=48000), OrderSide.BUY)]
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
        level = Mock(price=49000)
        grid_manager.get_initial_order_plan.return_value = [(level, OrderSide.BUY)]
        grid_manager.can_place_order.return_value = True
        order_validator.adjust_and_validate_buy_quantity.side_effect = ValueError("Insufficient balance")
        balance_tracker.balance = 0
        order_execution_strategy.execute_limit_order = AsyncMock()

        await manager.initialize_grid_orders(49500)

        order_execution_strategy.execute_limit_order.assert_not_awaited()
        grid_manager.can_place_order.assert_called_once_with(level, OrderSide.BUY)
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

        grid_manager.get_initial_order_plan.return_value = [(Mock(price=48000), OrderSide.BUY)]
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

        grid_manager.get_initial_order_plan.return_value = [(Mock(price=48000), OrderSide.BUY)]
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

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_skips_levels_that_still_hold_an_order(self, setup_order_manager):
        manager, grid_manager, order_validator, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        busy, free = Mock(price=49000), Mock(price=48000)
        grid_manager.get_initial_order_plan.return_value = [(busy, OrderSide.BUY), (free, OrderSide.BUY)]
        grid_manager.can_place_order.side_effect = lambda level, side: level is free
        order_validator.adjust_and_validate_buy_quantity.return_value = 0.01
        balance_tracker.balance = 1000
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=Mock())

        await manager.initialize_grid_orders(49500)

        order_execution_strategy.execute_limit_order.assert_awaited_once_with(
            OrderSide.BUY, manager.trading_pair, 0.01, 48000
        )

    # ── _handle_order_completion ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_handle_order_completion_buy(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        mock_order = Mock(side=OrderSide.BUY, filled=0.01)
        mock_grid_level = Mock(price=50000)
        target = Mock(price=52000)
        grid_manager.get_sell_target.return_value = target
        manager._place_order = AsyncMock()

        await manager._handle_order_completion(mock_order, mock_grid_level)

        grid_manager.complete_order.assert_called_once_with(mock_grid_level, OrderSide.BUY, False)
        manager._place_order.assert_awaited_once_with(OrderSide.SELL, mock_grid_level, target, 0.01)

    @pytest.mark.asyncio
    async def test_handle_order_completion_sell(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        mock_order = Mock(side=OrderSide.SELL, filled=0.01)
        mock_grid_level = Mock(price=50000)
        target = Mock(price=48000)
        grid_manager.get_buy_target.return_value = target
        manager._place_order = AsyncMock()

        await manager._handle_order_completion(mock_order, mock_grid_level)

        grid_manager.complete_order.assert_called_once_with(mock_grid_level, OrderSide.SELL, False)
        manager._place_order.assert_awaited_once_with(OrderSide.BUY, mock_grid_level, target, 0.01)

    @pytest.mark.asyncio
    async def test_handle_order_completion_buy_without_target_places_nothing(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        mock_grid_level = Mock(price=50000)
        grid_manager.get_sell_target.return_value = None
        manager._place_order = AsyncMock()

        await manager._handle_order_completion(Mock(side=OrderSide.BUY, filled=0.01), mock_grid_level)

        grid_manager.complete_order.assert_called_once_with(mock_grid_level, OrderSide.BUY, False)
        manager._place_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_order_completion_trusts_a_stacked_sell_target(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        mock_grid_level = Mock(price=95.0)
        stacked = GridLevel(price=105.0, state=GridCycleState.WAITING_FOR_SELL_FILL)
        grid_manager.get_sell_target.return_value = stacked
        grid_manager.can_place_order.return_value = False
        manager._place_order = AsyncMock()

        await manager._handle_order_completion(Mock(side=OrderSide.BUY, filled=0.5), mock_grid_level)

        manager._place_order.assert_awaited_once_with(OrderSide.SELL, mock_grid_level, stacked, 0.5)

    @pytest.mark.asyncio
    async def test_handle_order_completion_sell_without_target_places_nothing(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        mock_grid_level = Mock(price=50000)
        grid_manager.get_buy_target.return_value = None
        manager._place_order = AsyncMock()

        await manager._handle_order_completion(Mock(side=OrderSide.SELL, filled=0.1), mock_grid_level)

        grid_manager.complete_order.assert_called_once_with(mock_grid_level, OrderSide.SELL, False)
        manager._place_order.assert_not_awaited()

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
    async def test_perform_initial_purchase_backtest_updates_balances_once(self, setup_order_manager):
        manager, grid_manager, _, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        manager.trading_mode = TradingMode.BACKTEST
        mock_order = Mock(timestamp=1234567890)
        grid_manager.get_initial_order_quantity.return_value = 0.01
        order_execution_strategy.execute_market_order = AsyncMock(return_value=mock_order)

        await manager.perform_initial_purchase(50000)

        balance_tracker.update_after_initial_purchase.assert_awaited_once_with(initial_order=mock_order)

    @pytest.mark.asyncio
    async def test_perform_initial_purchase_live_calls_update_balance(self, setup_order_manager):
        manager, grid_manager, _, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        mock_order = Mock(timestamp=1234567890)
        grid_manager.get_initial_order_quantity.return_value = 0.01
        order_execution_strategy.execute_market_order = AsyncMock(return_value=mock_order)
        balance_tracker.update_after_initial_purchase = AsyncMock()

        await manager.perform_initial_purchase(50000)

        balance_tracker.update_after_initial_purchase.assert_awaited_once_with(initial_order=mock_order)

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
    async def test_execute_take_profit_or_stop_loss_order_no_action(self, setup_order_manager):
        manager, _, _, _, _, _, order_execution_strategy, _ = setup_order_manager

        await manager.execute_take_profit_or_stop_loss_order(50000)

        order_execution_strategy.execute_market_order.assert_not_called()

    # ── Buy/Sell Ratio ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_buy_fill_places_sell_with_sell_ratio(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        grid_manager.sell_ratio = 0.5
        mock_order = Mock(side=OrderSide.BUY, filled=1.0)
        mock_grid_level = Mock(price=50000)
        paired_sell_level = Mock(price=52000)
        grid_manager.get_sell_target.return_value = paired_sell_level
        manager._place_order = AsyncMock()

        await manager._handle_order_completion(mock_order, mock_grid_level)

        manager._place_order.assert_awaited_once_with(OrderSide.SELL, mock_grid_level, paired_sell_level, 0.5)

    @pytest.mark.asyncio
    async def test_buy_fill_places_sell_with_default_ratio(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        grid_manager.sell_ratio = 1.0
        mock_order = Mock(side=OrderSide.BUY, filled=1.0)
        mock_grid_level = Mock(price=50000)
        paired_sell_level = Mock(price=52000)
        grid_manager.get_sell_target.return_value = paired_sell_level
        manager._place_order = AsyncMock()

        await manager._handle_order_completion(mock_order, mock_grid_level)

        manager._place_order.assert_awaited_once_with(OrderSide.SELL, mock_grid_level, paired_sell_level, 1.0)

    @pytest.mark.asyncio
    async def test_sell_fill_places_buy_with_default_ratio(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        grid_manager.buy_ratio = 1.0
        mock_order = Mock(side=OrderSide.SELL, filled=0.5)
        mock_grid_level = Mock(price=52000)
        paired_buy_level = Mock(price=50000)
        grid_manager.get_buy_target.return_value = paired_buy_level
        manager._place_order = AsyncMock()

        await manager._handle_order_completion(mock_order, mock_grid_level)

        # buy_qty = 0.5 * 1.0 = 0.5 (same as filled, backward compatible)
        manager._place_order.assert_awaited_once_with(OrderSide.BUY, mock_grid_level, paired_buy_level, 0.5)

    @pytest.mark.asyncio
    async def test_sell_fill_places_buy_with_buy_ratio_half(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        grid_manager.buy_ratio = 0.5
        mock_order = Mock(side=OrderSide.SELL, filled=1.0)
        mock_grid_level = Mock(price=52000)
        paired_buy_level = Mock(price=50000)
        grid_manager.get_buy_target.return_value = paired_buy_level
        manager._place_order = AsyncMock()

        await manager._handle_order_completion(mock_order, mock_grid_level)

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
        grid_manager.rollback_order_placement.assert_called_once_with(grid_level, OrderSide.BUY, False)
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
        grid_manager.rollback_order_placement.assert_called_once_with(grid_level, OrderSide.BUY, False)

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
        grid_manager.rollback_order_placement.assert_called_once_with(grid_level, OrderSide.BUY, False)

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
        grid_manager.rollback_order_placement.assert_called_once_with(sell_level, OrderSide.SELL, False)

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
        grid_manager.rollback_order_placement.assert_called_once_with(lvl1, OrderSide.BUY, False)

    @pytest.mark.asyncio
    async def test_no_open_orders_is_success(self, setup_order_manager):
        manager, _, _, _, order_book, _, _, _ = setup_order_manager
        order_book.get_buy_orders_with_grid.return_value = []
        order_book.get_sell_orders_with_grid.return_value = []
        order_book.get_open_orders.return_value = []

        assert await manager.cancel_open_grid_orders() is True

    @pytest.mark.asyncio
    async def test_stacked_level_stays_waiting_until_its_last_order_is_cancelled(self, setup_order_manager):
        manager, grid_manager, _, _, order_book, _, order_execution_strategy, _ = setup_order_manager
        manager.trading_mode = TradingMode.BACKTEST
        level = Mock(price=105.0)
        first = Mock(identifier="s1", side=OrderSide.SELL, amount=1.0, filled=0.0, remaining=1.0, price=105.0)
        second = Mock(identifier="s2", side=OrderSide.SELL, amount=1.0, filled=0.0, remaining=1.0, price=105.0)
        order_book.get_buy_orders_with_grid.return_value = []
        order_book.get_sell_orders_with_grid.return_value = [(first, level), (second, level)]
        order_book.get_open_orders.return_value = [first, second]
        order_book.has_open_orders_at.side_effect = [True, False]
        order_execution_strategy.cancel_order = AsyncMock(return_value=True)

        assert await manager.cancel_open_grid_orders() is True

        assert grid_manager.rollback_order_placement.call_args_list == [
            call(level, OrderSide.SELL, True),
            call(level, OrderSide.SELL, False),
        ]


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

    # ── _initialize_order defensive skip ────────────────────────────────

    @pytest.mark.asyncio
    async def test_initialize_orders_skips_dust_level(self, setup_order_manager):
        manager, grid_manager, order_validator, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        manager.set_market_constraints(MarketConstraints(min_amount=1.0, min_cost=None))
        grid_manager.get_initial_order_plan.return_value = [(Mock(price=48000), OrderSide.BUY)]
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
        grid_manager.rollback_order_placement.assert_called_once_with(grid_level, OrderSide.BUY, False)
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
        grid_manager.rollback_order_placement.assert_called_once_with(grid_level, OrderSide.SELL, False)
        grid_manager.mark_order_pending.assert_called_once_with(grid_level, new_order)
        order_book.add_order.assert_called_once_with(new_order, grid_level)

    @pytest.mark.asyncio
    async def test_settles_but_does_not_replace_after_positions_are_closed(self, setup_order_manager):
        manager, grid_manager, order_validator, balance_tracker, order_book, _, order_execution_strategy, _ = (
            setup_order_manager
        )
        grid_level = GridLevel(price=105.0, state=GridCycleState.WAITING_FOR_SELL_FILL)
        order = _mock_order(side=OrderSide.SELL, amount=1.0, filled=0.0, remaining=1.0, price=105.0)
        order_book.get_grid_level_for_order.return_value = grid_level
        order_validator.adjust_and_validate_sell_quantity.return_value = 1.0
        order_execution_strategy.execute_limit_order = AsyncMock()
        manager._positions_closed = True

        await manager._on_order_cancelled(order)

        balance_tracker.settle_cancelled_order.assert_awaited_once_with(order)
        grid_manager.rollback_order_placement.assert_called_once_with(grid_level, OrderSide.SELL, False)
        order_execution_strategy.execute_limit_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_an_order_already_settled_by_bulk_cancel(self, setup_order_manager):
        manager, _, _, balance_tracker, order_book, _, order_execution_strategy, _ = setup_order_manager
        order = _mock_order(side=OrderSide.BUY, identifier="b1")
        grid_level = GridLevel(price=95.0, state=GridCycleState.WAITING_FOR_BUY_FILL)
        order_book.get_buy_orders_with_grid.return_value = [(order, grid_level)]
        order_book.get_sell_orders_with_grid.return_value = []
        order_book.get_open_orders.return_value = [order]
        order_book.get_grid_level_for_order.return_value = grid_level
        order_execution_strategy.cancel_order = AsyncMock(return_value=True)
        order_execution_strategy.get_order = AsyncMock(return_value=order)
        order_execution_strategy.execute_limit_order = AsyncMock()

        await manager.cancel_open_grid_orders()
        await manager._on_order_cancelled(order)

        balance_tracker.settle_cancelled_order.assert_awaited_once_with(order)
        order_execution_strategy.execute_limit_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_settles_a_duplicate_cancel_event_once(self, setup_order_manager):
        manager, _, order_validator, balance_tracker, order_book, _, order_execution_strategy, _ = setup_order_manager
        grid_level = GridLevel(price=95.0, state=GridCycleState.WAITING_FOR_BUY_FILL)
        order = _mock_order(side=OrderSide.BUY, identifier="b1")
        order_book.get_grid_level_for_order.return_value = grid_level
        order_validator.adjust_and_validate_buy_quantity.return_value = 1.0
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=_mock_order(identifier="b2"))

        await manager._on_order_cancelled(order)
        await manager._on_order_cancelled(order)

        balance_tracker.settle_cancelled_order.assert_awaited_once_with(order)
        order_execution_strategy.execute_limit_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_settles_even_when_the_level_state_was_overwritten(self, setup_order_manager):
        manager, _, order_validator, balance_tracker, order_book, _, order_execution_strategy, _ = setup_order_manager
        grid_level = GridLevel(price=95.0, state=GridCycleState.READY_TO_BUY_OR_SELL)
        order = _mock_order(side=OrderSide.BUY, identifier="b1")
        order_book.get_grid_level_for_order.return_value = grid_level
        order_validator.adjust_and_validate_buy_quantity.return_value = 1.0
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=_mock_order(identifier="b2"))

        await manager._on_order_cancelled(order)

        balance_tracker.settle_cancelled_order.assert_awaited_once_with(order)

    @pytest.mark.asyncio
    async def test_cancel_on_a_level_holding_other_orders_keeps_it_waiting(self, setup_order_manager):
        manager, grid_manager, order_validator, _, order_book, _, order_execution_strategy, _ = setup_order_manager
        grid_level = GridLevel(price=105.0, state=GridCycleState.WAITING_FOR_SELL_FILL)
        order = _mock_order(side=OrderSide.SELL, identifier="s1", price=105.0)
        order_book.get_grid_level_for_order.return_value = grid_level
        order_book.has_open_orders_at.return_value = True
        order_validator.adjust_and_validate_sell_quantity.return_value = 1.0
        order_execution_strategy.execute_limit_order = AsyncMock(return_value=_mock_order(identifier="s2"))

        await manager._on_order_cancelled(order)

        grid_manager.rollback_order_placement.assert_called_once_with(grid_level, OrderSide.SELL, True)

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


class TestTakeProfitStopLoss:
    # ── execute_take_profit_or_stop_loss_order (every mode) ─────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trading_mode", [TradingMode.BACKTEST, TradingMode.PAPER_TRADING, TradingMode.LIVE])
    async def test_tp_delegates_to_close_positions(self, setup_order_manager, trading_mode):
        manager, *_ = setup_order_manager
        manager.trading_mode = trading_mode
        manager.close_positions = AsyncMock()

        await manager.execute_take_profit_or_stop_loss_order(current_price=100.0, take_profit_order=True)

        manager.close_positions.assert_awaited_once_with(liquidate=True, current_price=100.0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trading_mode", [TradingMode.BACKTEST, TradingMode.PAPER_TRADING, TradingMode.LIVE])
    async def test_sl_delegates_to_close_positions_and_notifies(self, setup_order_manager, trading_mode):
        manager, _, _, _, _, _, _, notification_handler = setup_order_manager
        manager.trading_mode = trading_mode
        manager.close_positions = AsyncMock()

        await manager.execute_take_profit_or_stop_loss_order(current_price=90.0, stop_loss_order=True)

        manager.close_positions.assert_awaited_once_with(liquidate=True, current_price=90.0)
        notification_handler.async_send_notification.assert_awaited_once_with(
            NotificationType.STOP_LOSS_TRIGGERED,
            order_details="Stop loss at 90.0: open orders cancelled, position liquidated.",
        )


class TestOrderManagerPostOnlyRejection:
    """
    A maker-only order refused because it would cross means the grid level is simply not
    placeable right now. It must release its reservation, leave the level untouched so the
    next `initialize_grid_orders` retries it, and not be reported as a failed order.
    """

    @pytest.mark.asyncio
    async def test_initialize_releases_the_reservation(self, setup_order_manager):
        manager, grid_manager, order_validator, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.get_initial_order_plan.return_value = [(Mock(price=49000), OrderSide.BUY)]
        grid_manager.can_place_order.return_value = True
        order_validator.adjust_and_validate_buy_quantity.return_value = 0.01
        balance_tracker.balance = 1000
        order_execution_strategy.execute_limit_order = AsyncMock(side_effect=PostOnlyRejectedError("would cross"))

        await manager.initialize_grid_orders(49500)

        balance_tracker.release_reserved_fiat.assert_awaited_once_with(0.01 * 49000)

    @pytest.mark.asyncio
    async def test_initialize_does_not_report_a_failure(self, setup_order_manager):
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
        grid_manager.get_initial_order_plan.return_value = [(Mock(price=49000), OrderSide.BUY)]
        grid_manager.can_place_order.return_value = True
        order_validator.adjust_and_validate_buy_quantity.return_value = 0.01
        balance_tracker.balance = 1000
        order_execution_strategy.execute_limit_order = AsyncMock(side_effect=PostOnlyRejectedError("would cross"))

        await manager.initialize_grid_orders(49500)

        notification_handler.async_send_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_initialize_leaves_the_level_placeable(self, setup_order_manager):
        manager, grid_manager, order_validator, balance_tracker, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.get_initial_order_plan.return_value = [(Mock(price=49000), OrderSide.BUY)]
        grid_manager.can_place_order.return_value = True
        order_validator.adjust_and_validate_buy_quantity.return_value = 0.01
        balance_tracker.balance = 1000
        order_execution_strategy.execute_limit_order = AsyncMock(side_effect=PostOnlyRejectedError("would cross"))

        await manager.initialize_grid_orders(49500)

        grid_manager.mark_order_pending.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_rejected_level_does_not_stop_the_others(self, setup_order_manager):
        manager, grid_manager, order_validator, balance_tracker, order_book, _, order_execution_strategy, _ = (
            setup_order_manager
        )
        levels = [Mock(price=49000), Mock(price=48000)]
        grid_manager.get_initial_order_plan.return_value = [(level, OrderSide.BUY) for level in levels]
        grid_manager.can_place_order.side_effect = lambda level, side: side == OrderSide.BUY
        order_validator.adjust_and_validate_buy_quantity.return_value = 0.01
        balance_tracker.balance = 1000
        placed = Mock()
        order_execution_strategy.execute_limit_order = AsyncMock(
            side_effect=[PostOnlyRejectedError("would cross"), placed],
        )

        await manager.initialize_grid_orders(49500)

        assert order_execution_strategy.execute_limit_order.await_count == 2
        order_book.add_order.assert_called_once_with(placed, levels[1])

    @pytest.mark.asyncio
    async def test_paired_order_rejection_releases_funds_and_stays_unpaired(self, setup_order_manager):
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
        source = GridLevel(price=95.0, state=GridCycleState.READY_TO_SELL)
        target = GridLevel(price=105.0, state=GridCycleState.READY_TO_SELL)
        order_validator.adjust_and_validate_sell_quantity.return_value = 0.5
        balance_tracker.crypto_balance = 1
        order_execution_strategy.execute_limit_order = AsyncMock(side_effect=PostOnlyRejectedError("would cross"))

        await manager._place_order(OrderSide.SELL, source, target, 0.5)

        balance_tracker.release_reserved_crypto.assert_awaited_once_with(0.5)
        grid_manager.pair_grid_levels.assert_not_called()
        grid_manager.mark_order_pending.assert_not_called()
        notification_handler.async_send_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancelled_replacement_rejection_releases_funds_without_reporting(self, setup_order_manager):
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
        grid_level = GridLevel(price=95.0, state=GridCycleState.WAITING_FOR_BUY_FILL)
        order = _mock_order(side=OrderSide.BUY, amount=1.0, filled=0.0, remaining=1.0, price=95.0)
        order_book.get_grid_level_for_order.return_value = grid_level
        order_validator.adjust_and_validate_buy_quantity.return_value = 1.0
        order_execution_strategy.execute_limit_order = AsyncMock(side_effect=PostOnlyRejectedError("would cross"))

        await manager._on_order_cancelled(order)

        balance_tracker.release_reserved_fiat.assert_awaited_once_with(1.0 * 95.0)
        grid_manager.mark_order_pending.assert_not_called()
        # The ORDER_CANCELLED notification is expected; an error report on top of it is not.
        sent = [c.args[0] for c in notification_handler.async_send_notification.await_args_list]
        assert NotificationType.ORDER_CANCELLED in sent
        assert NotificationType.ORDER_FAILED not in sent
        assert NotificationType.ERROR_OCCURRED not in sent


class TestPlacePairedOrdersForRecoveredFills:
    @pytest.mark.asyncio
    async def test_places_the_paired_order_of_each_recovered_fill(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, _ = setup_order_manager
        buy_level, sell_level = Mock(price=95.0), Mock(price=105.0)
        sell_target, buy_target = Mock(price=105.0), Mock(price=95.0)
        grid_manager.get_sell_target.return_value = sell_target
        grid_manager.get_buy_target.return_value = buy_target
        manager._place_order = AsyncMock()
        buy_fill = Mock(side=OrderSide.BUY, filled=1.0, identifier="b")
        sell_fill = Mock(side=OrderSide.SELL, filled=0.5, identifier="s")

        await manager.place_paired_orders_for_recovered_fills([(buy_fill, buy_level), (sell_fill, sell_level)])

        assert manager._place_order.await_args_list == [
            call(OrderSide.SELL, buy_level, sell_target, 1.0),
            call(OrderSide.BUY, sell_level, buy_target, 0.5),
        ]
        grid_manager.complete_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_failure_does_not_stop_the_next_fill(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, _, notification_handler = setup_order_manager
        grid_manager.get_sell_target.return_value = Mock(price=105.0)
        manager._place_order = AsyncMock(side_effect=[RuntimeError("boom"), None])
        fills = [
            (Mock(side=OrderSide.BUY, filled=1.0, identifier="b1"), Mock(price=95.0)),
            (Mock(side=OrderSide.BUY, filled=1.0, identifier="b2"), Mock(price=96.0)),
        ]

        await manager.place_paired_orders_for_recovered_fills(fills)

        assert manager._place_order.await_count == 2
        sent = [c.args[0] for c in notification_handler.async_send_notification.await_args_list]
        assert NotificationType.ERROR_OCCURRED in sent


class TestNoNewOrderOnceClosed:
    @pytest.mark.asyncio
    async def test_initial_purchase_sends_nothing(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.get_initial_order_quantity.return_value = 0.01
        order_execution_strategy.execute_market_order = AsyncMock()
        manager._positions_closed = True

        await manager.perform_initial_purchase(50000)

        order_execution_strategy.execute_market_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_sends_nothing(self, setup_order_manager):
        manager, grid_manager, order_validator, _, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.get_initial_order_plan.return_value = [(Mock(price=49000), OrderSide.BUY)]
        grid_manager.can_place_order.return_value = True
        order_validator.adjust_and_validate_buy_quantity.return_value = 0.01
        order_execution_strategy.execute_limit_order = AsyncMock()
        manager._positions_closed = True

        await manager.initialize_grid_orders(49500)

        order_execution_strategy.execute_limit_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_stops_when_a_close_lands_mid_loop(self, setup_order_manager):
        manager, grid_manager, order_validator, _, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.get_initial_order_plan.return_value = [
            (Mock(price=49000), OrderSide.BUY),
            (Mock(price=48000), OrderSide.BUY),
            (Mock(price=47000), OrderSide.BUY),
        ]
        grid_manager.can_place_order.return_value = True
        order_validator.adjust_and_validate_buy_quantity.return_value = 0.01

        async def place_then_close(*args, **kwargs):
            manager._positions_closed = True
            return Mock()

        order_execution_strategy.execute_limit_order = AsyncMock(side_effect=place_then_close)

        await manager.initialize_grid_orders(49500)

        assert order_execution_strategy.execute_limit_order.await_count == 1

    @pytest.mark.asyncio
    async def test_place_order_sends_nothing(self, setup_order_manager):
        manager, _, _, _, _, _, order_execution_strategy, _ = setup_order_manager
        order_execution_strategy.execute_limit_order = AsyncMock()
        manager._positions_closed = True

        await manager._place_order(OrderSide.SELL, Mock(price=95.0), Mock(price=105.0), 1.0)

        order_execution_strategy.execute_limit_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recovered_fill_pairing_sends_nothing(self, setup_order_manager):
        manager, grid_manager, _, _, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.get_sell_target.return_value = Mock(price=105.0)
        order_execution_strategy.execute_limit_order = AsyncMock()
        manager._positions_closed = True
        fill = Mock(side=OrderSide.BUY, filled=1.0, identifier="b")

        await manager.place_paired_orders_for_recovered_fills([(fill, Mock(price=95.0))])

        order_execution_strategy.execute_limit_order.assert_not_awaited()


class RecordingStore:
    def __init__(self, manager_ref):
        self.manager_ref = manager_ref
        self.saves = []

    async def save(self, arming):
        self.saves.append((arming, self.manager_ref()._lock.locked()))


@pytest.fixture
def manager_with_store(setup_order_manager):
    manager = setup_order_manager[0]
    store = RecordingStore(lambda: manager)
    manager.state_store = store
    return manager, store, setup_order_manager


class TestStateSaves:
    async def test_initialize_grid_orders_saves_under_the_lock_with_the_grid_flag(self, manager_with_store):
        manager, store, (_, grid_manager, *_rest) = manager_with_store
        grid_manager.get_initial_order_plan.return_value = []

        await manager.initialize_grid_orders(100.0)

        assert store.saves == [(ArmingFlags(grid_orders_initialized=True), True)]

    async def test_the_initial_purchase_saves_its_flag_even_when_it_buys_nothing(self, manager_with_store):
        manager, store, (_, grid_manager, *_rest) = manager_with_store
        grid_manager.get_initial_order_quantity.return_value = 0

        await manager.perform_initial_purchase(100.0)

        assert store.saves == [(ArmingFlags(initial_purchase_done=True), True)]

    async def test_a_done_initial_purchase_is_never_repeated(self, manager_with_store):
        manager, store, (*_, order_execution_strategy, _) = manager_with_store
        manager.restore_arming(ArmingFlags(initial_purchase_done=True))
        order_execution_strategy.execute_market_order = AsyncMock()

        await manager.perform_initial_purchase(100.0)

        order_execution_strategy.execute_market_order.assert_not_awaited()
        assert store.saves == []

    async def test_a_bulk_cancellation_saves(self, manager_with_store):
        manager, store, (_, _, _, _, order_book, *_rest) = manager_with_store
        order_book.get_open_orders.return_value = []
        order_book.get_buy_orders_with_grid.return_value = []
        order_book.get_sell_orders_with_grid.return_value = []

        assert await manager.cancel_open_grid_orders() is True

        assert len(store.saves) == 1

    async def test_recovered_pairs_save_once_after_placing(self, manager_with_store):
        manager, store, _ = manager_with_store
        manager._place_paired_order = AsyncMock()

        await manager.place_paired_orders_for_recovered_fills([(Mock(), Mock()), (Mock(), Mock())])

        assert manager._place_paired_order.await_count == 2
        assert len(store.saves) == 1

    async def test_a_failed_grid_placement_still_saves_what_was_placed(self, manager_with_store):
        manager, store, (_, grid_manager, *_rest) = manager_with_store
        grid_manager.get_initial_order_plan.side_effect = RuntimeError("plan broken")

        with pytest.raises(RuntimeError):
            await manager.initialize_grid_orders(100.0)

        assert store.saves == [(ArmingFlags(), True)]

    async def test_checkpoint_saves_under_the_lock(self, manager_with_store):
        manager, store, _ = manager_with_store

        await manager.checkpoint()

        assert store.saves == [(ArmingFlags(), True)]

    async def test_a_checkpoint_gives_up_when_the_lock_stays_held(self, manager_with_store):
        manager, store, _ = manager_with_store
        await manager._lock.acquire()
        try:
            await asyncio.wait_for(manager.checkpoint(lock_timeout=0.05), timeout=1)
        finally:
            manager._lock.release()

        assert store.saves == []

    async def test_the_liquidation_saves_once_under_the_lock_after_the_market_sell(self, manager_with_store):
        manager, store, (_, _, _, balance_tracker, order_book, _, order_execution_strategy, _) = manager_with_store
        order_book.get_open_orders.return_value = []
        order_book.get_buy_orders_with_grid.return_value = []
        order_book.get_sell_orders_with_grid.return_value = []
        balance_tracker.crypto_balance = 1.0
        order_execution_strategy.execute_market_order = AsyncMock(
            return_value=_mock_order(side=OrderSide.SELL, status=OrderStatus.CLOSED, filled=1.0, amount=1.0)
        )
        events = []
        balance_tracker.update_after_liquidation = AsyncMock(side_effect=lambda *_a: events.append("booked"))
        original_save = store.save

        async def recording_save(arming):
            events.append("saved")
            await original_save(arming)

        store.save = recording_save

        await manager.close_positions(liquidate=True, current_price=100.0)

        assert [locked for _, locked in store.saves] == [True, True]
        assert events == ["saved", "booked", "saved"]

    async def test_without_a_store_nothing_is_saved(self, setup_order_manager):
        manager = setup_order_manager[0]

        await manager.checkpoint()

        assert not manager._lock.locked()


class TestRegrid:
    @pytest.fixture
    def steps(self, manager_with_store):
        manager, _store, _ = manager_with_store
        recorded = []

        async def cancel():
            recorded.append(("cancel", manager._lock.locked()))
            return manager.cancel_succeeds

        async def place(price):
            recorded.append((f"place {price}", manager._lock.locked()))

        manager.cancel_succeeds = True
        manager._cancel_open_grid_orders = cancel
        manager._place_initial_orders = place
        return recorded

    async def test_a_regrid_cancels_rebuilds_and_places_under_one_lock_and_saves_once(self, manager_with_store, steps):
        manager, store, _ = manager_with_store

        rebuilt = await manager.regrid(100.0, lambda: steps.append(("rebuild", manager._lock.locked())))

        assert rebuilt is True
        assert steps == [("cancel", True), ("rebuild", True), ("place 100.0", True)]
        assert len(store.saves) == 1

    async def test_a_failed_cancellation_replaces_on_the_current_grid(self, manager_with_store, steps):
        manager, store, _ = manager_with_store
        manager.cancel_succeeds = False

        rebuilt = await manager.regrid(100.0, lambda: steps.append(("rebuild", True)))

        assert rebuilt is False
        assert steps == [("cancel", True), ("place 100.0", True)]
        assert len(store.saves) == 1

    async def test_a_rejected_rebuild_replaces_on_the_existing_grid(self, manager_with_store, steps):
        manager, store, _ = manager_with_store

        def rejected_rebuild():
            steps.append(("rebuild", True))
            raise ValueError("Regrid would produce non-positive bottom price")

        rebuilt = await manager.regrid(100.0, rejected_rebuild)

        assert rebuilt is False
        assert steps == [("cancel", True), ("rebuild", True), ("place 100.0", True)]
        assert len(store.saves) == 1
