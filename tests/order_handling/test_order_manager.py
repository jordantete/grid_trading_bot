import asyncio
from unittest.mock import AsyncMock, Mock, call

import pytest

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.order_handling.exceptions import GridFeasibilityError
from grid_trading_bot.core.order_handling.order import OrderSide
from grid_trading_bot.core.order_handling.state_store import ArmingFlags
from grid_trading_bot.core.services.market_constraints import MarketConstraints


class TestOrderManager:
    # ── execute_take_profit_or_stop_loss_order ──────────────────────────

    @pytest.mark.asyncio
    async def test_execute_take_profit_or_stop_loss_order_no_action(self, setup_order_manager):
        manager, _, _, _, _, order_execution_strategy, _ = setup_order_manager

        await manager.execute_take_profit_or_stop_loss_order(50000)

        order_execution_strategy.execute_market_order.assert_not_called()


class TestCancelOpenGridOrders:
    # ── cancel_open_grid_orders ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_no_open_orders_is_success(self, setup_order_manager):
        manager, _, _, _, order_book, _, _ = setup_order_manager
        order_book.get_buy_orders_with_grid.return_value = []
        order_book.get_sell_orders_with_grid.return_value = []
        order_book.get_open_orders.return_value = []

        assert await manager.cancel_open_grid_orders() is True

    @pytest.mark.asyncio
    async def test_stacked_level_stays_waiting_until_its_last_order_is_cancelled(self, setup_order_manager):
        manager, grid_manager, _, _, order_book, order_execution_strategy, _ = setup_order_manager
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
        manager, grid_manager, _, balance_tracker, _, _, _ = setup_order_manager
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
        manager, grid_manager, _, balance_tracker, _, _, _ = setup_order_manager
        manager.set_market_constraints(MarketConstraints(min_amount=None, min_cost=50.0))
        grid_manager.sorted_buy_grids = [90.0, 95.0]
        grid_manager.sorted_sell_grids = [105.0, 110.0]
        balance_tracker.get_total_balance_value.return_value = 100000.0
        grid_manager.get_order_size_for_grid_level.return_value = 1.0  # notional ~100 > 50

        manager.validate_grid_feasibility(current_price=100.0)  # must not raise


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
        manager, _, _, _, _, _, notification_handler = setup_order_manager
        manager.trading_mode = trading_mode

        async def liquidated(**_kwargs):
            manager._liquidated = True
            manager.balance_tracker.crypto_balance = 0.0

        manager.close_positions = AsyncMock(side_effect=liquidated)

        await manager.execute_take_profit_or_stop_loss_order(current_price=90.0, stop_loss_order=True)

        manager.close_positions.assert_awaited_once_with(liquidate=True, current_price=90.0)
        notification_handler.async_send_notification.assert_awaited_once_with(
            NotificationType.STOP_LOSS_TRIGGERED,
            order_details="Stop loss at 90.0: open orders cancelled, position liquidated.",
        )

    @pytest.mark.asyncio
    async def test_an_incomplete_liquidation_says_so(self, setup_order_manager):
        manager, _, _, _, _, _, notification_handler = setup_order_manager
        manager.close_positions = AsyncMock()

        await manager.execute_take_profit_or_stop_loss_order(current_price=90.0, stop_loss_order=True)

        notification_handler.async_send_notification.assert_awaited_once_with(
            NotificationType.STOP_LOSS_TRIGGERED,
            order_details=(
                "Stop loss at 90.0: open orders cancelled, liquidation incomplete (see the liquidation alerts)."
            ),
        )


class TestNoNewOrderOnceClosed:
    @pytest.mark.asyncio
    async def test_initialize_grid_orders_sends_nothing(self, setup_order_manager):
        manager, grid_manager, order_validator, _, _, order_execution_strategy, _ = setup_order_manager
        grid_manager.get_initial_order_plan.return_value = [(Mock(price=49000), OrderSide.BUY)]
        grid_manager.can_place_order.return_value = True
        order_validator.adjust_and_validate_buy_quantity.return_value = 0.01
        order_execution_strategy.execute_limit_order = AsyncMock()
        manager._positions_closed = True

        await manager.initialize_grid_orders(49500)

        order_execution_strategy.execute_limit_order.assert_not_awaited()


class RecordingStore:
    def __init__(self, manager_ref):
        self.manager_ref = manager_ref
        self.saves = []

    async def save(self, arming):
        self.saves.append((arming, self.manager_ref()._lock.locked()))

    async def save_pending(self, _entry):
        return True


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
