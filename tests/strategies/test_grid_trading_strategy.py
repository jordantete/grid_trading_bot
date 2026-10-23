from unittest.mock import AsyncMock, Mock

import pytest
from tests.fakes.config import config_manager_from
from tests.fixtures.strategies import use_dynamic_spacing, use_trailing_stop

from grid_trading_bot.config.schema import TrailingTrigger
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest
from grid_trading_bot.core.exceptions import BookingError
from grid_trading_bot.core.order_handling.exceptions import GridFeasibilityError
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus, OrderType
from grid_trading_bot.core.risk_management.trailing_stop_loss import TrailingStopLoss
from grid_trading_bot.strategies.backtest_grid_strategy import BacktestGridStrategy
from grid_trading_bot.strategies.live_grid_strategy import LiveGridStrategy


def _order_manager_regrid(rebuilt=True):
    """Stands in for OrderManager.regrid: runs the rebuild only when the cancellation would have succeeded."""

    async def regrid(_center_price, rebuild):
        if rebuilt:
            rebuild()
        return rebuilt

    return AsyncMock(side_effect=regrid)


def _executed_buy() -> Order:
    return Order(
        identifier="o1",
        status=OrderStatus.CLOSED,
        order_type=OrderType.MARKET,
        side=OrderSide.BUY,
        price=15100.0,
        average=15100.0,
        amount=0.1,
        filled=0.1,
        remaining=0.0,
        timestamp=0,
        datetime=None,
        last_trade_timestamp=None,
        symbol="BTC/USDT",
        time_in_force="GTC",
    )


class TestGridTradingStrategy:
    @pytest.mark.asyncio
    async def test_initialize_strategy(self, setup_strategy):
        create_strategy, _, _, grid_manager, *_ = setup_strategy
        strategy = create_strategy()

        strategy.initialize_strategy()

        grid_manager.initialize_grids_and_levels.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_trading(self, setup_strategy):
        create_strategy, _, exchange_service, *_ = setup_strategy
        strategy = create_strategy()
        exchange_service.close_connection = AsyncMock()

        await strategy.stop()

        assert strategy._running is False
        exchange_service.close_connection.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("strategy_class", "mode_label"),
        [(LiveGridStrategy, "live"), (LiveGridStrategy, "paper"), (BacktestGridStrategy, None)],
    )
    async def test_stop_leaves_orders_and_the_exchange_to_the_bot(self, strategy_class, mode_label, setup_strategy):
        create_strategy, _, exchange_service, _, order_manager, *_ = setup_strategy
        strategy = create_strategy(strategy_class, mode_label)
        exchange_service.close_connection = AsyncMock()
        exchange_service.close = AsyncMock()
        order_manager.close_positions = AsyncMock()

        await strategy.stop()

        assert strategy._running is False
        exchange_service.close_connection.assert_awaited_once()
        exchange_service.close.assert_not_awaited()
        order_manager.close_positions.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_evaluate_tp_or_sl_no_crypto_balance(self, setup_strategy):
        create_strategy, _, _, _, _, balance_tracker, _, _, _ = setup_strategy
        strategy = create_strategy()

        balance_tracker.crypto_balance = 0
        balance_tracker.get_adjusted_crypto_balance.return_value = 0

        result = await strategy._evaluate_tp_or_sl(current_price=15000)
        assert result is False

    @pytest.mark.asyncio
    async def test_evaluate_tp_or_sl_checks_crypto_reserved_in_sell_orders(self, setup_strategy):
        create_strategy, _, _, _, order_manager, balance_tracker, _, _, _ = setup_strategy
        strategy = create_strategy()
        balance_tracker.crypto_balance = 0
        balance_tracker.get_adjusted_crypto_balance.return_value = 1.5
        order_manager.execute_take_profit_or_stop_loss_order = AsyncMock()

        result = await strategy._evaluate_tp_or_sl(current_price=9000)

        assert result is True
        order_manager.execute_take_profit_or_stop_loss_order.assert_awaited_once_with(
            current_price=9000,
            stop_loss_order=True,
        )

    @pytest.mark.asyncio
    async def test_handle_take_profit_triggered(self, setup_strategy):
        create_strategy, _, _, _, order_manager, balance_tracker, _, _, _ = setup_strategy
        strategy = create_strategy()

        balance_tracker.crypto_balance = 1
        order_manager.execute_take_profit_or_stop_loss_order = AsyncMock()

        result = await strategy._handle_take_profit(current_price=21000)

        assert result is True
        order_manager.execute_take_profit_or_stop_loss_order.assert_called_once_with(
            current_price=21000,
            take_profit_order=True,
        )

    @pytest.mark.asyncio
    async def test_handle_stop_loss_triggered(self, setup_strategy):
        create_strategy, _, _, _, order_manager, balance_tracker, _, _, _ = setup_strategy
        strategy = create_strategy()

        balance_tracker.crypto_balance = 1
        order_manager.execute_take_profit_or_stop_loss_order = AsyncMock()

        result = await strategy._handle_stop_loss(current_price=9000)

        assert result is True
        order_manager.execute_take_profit_or_stop_loss_order.assert_called_once_with(
            current_price=9000,
            stop_loss_order=True,
        )

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_once_arms_when_price_in_range(self, setup_strategy):
        create_strategy, _, _, grid_manager, order_manager, _, _, _, _ = setup_strategy
        strategy = create_strategy()

        grid_manager.is_within_grid_range.return_value = True
        order_manager.perform_initial_purchase = AsyncMock()
        order_manager.initialize_grid_orders = AsyncMock()

        result = await strategy._initialize_grid_orders_once(
            current_price=15100,
            grid_orders_initialized=False,
        )

        assert result is True
        grid_manager.is_within_grid_range.assert_called_once_with(15100)
        order_manager.perform_initial_purchase.assert_called_once_with(15100)
        order_manager.initialize_grid_orders.assert_called_once_with(15100)

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_once_waits_for_an_initial_purchase_not_done(self, setup_strategy):
        create_strategy, _, _, grid_manager, order_manager, _, _, _, _ = setup_strategy
        strategy = create_strategy()

        grid_manager.is_within_grid_range.return_value = True
        order_manager.perform_initial_purchase = AsyncMock(return_value=False)
        order_manager.initialize_grid_orders = AsyncMock()

        result = await strategy._initialize_grid_orders_once(current_price=15100, grid_orders_initialized=False)

        assert result is False
        assert strategy.armed is False
        order_manager.initialize_grid_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_booking_error_on_the_initial_purchase_halts_live(self, setup_strategy):
        create_strategy, _, _, grid_manager, order_manager, *_ = setup_strategy
        strategy = create_strategy(LiveGridStrategy)
        bus = EventBus()
        requests: list[StopRequest] = []

        async def record(request):
            requests.append(request)

        bus.subscribe(Events.STOP_BOT, record)
        strategy.event_bus = bus
        grid_manager.is_within_grid_range.return_value = True
        order_manager.perform_initial_purchase = AsyncMock(
            side_effect=BookingError(_executed_buy(), ValueError("could not convert string to float: 'n/a'"))
        )
        order_manager.initialize_grid_orders = AsyncMock()

        result = await strategy._initialize_grid_orders_once(current_price=15100, grid_orders_initialized=False)

        assert result is False
        assert strategy.armed is False
        assert strategy.stop_reason == "booking_error"
        [request] = requests
        assert request.kind is StopKind.RISK
        assert request.reason.startswith("Initial purchase not booked: buy market order o1 executed (0.1 at 15100.0")

    @pytest.mark.asyncio
    async def test_a_booking_error_on_the_initial_purchase_fails_a_backtest(self, setup_strategy):
        create_strategy, _, _, grid_manager, order_manager, *_ = setup_strategy
        strategy = create_strategy(BacktestGridStrategy)
        grid_manager.is_within_grid_range.return_value = True
        order_manager.perform_initial_purchase = AsyncMock(side_effect=BookingError(_executed_buy(), ValueError("bad")))

        with pytest.raises(BookingError):
            await strategy._initialize_grid_orders_once(current_price=15100, grid_orders_initialized=False)

        assert strategy.stop_reason is None

    @pytest.mark.asyncio
    async def test_the_booking_error_policy_follows_fail_fast_not_the_strategy_class(self, setup_strategy):
        create_strategy, _, _, grid_manager, order_manager, *_ = setup_strategy
        strategy = create_strategy(LiveGridStrategy)
        order_manager.fail_fast = True
        grid_manager.is_within_grid_range.return_value = True
        order_manager.perform_initial_purchase = AsyncMock(side_effect=BookingError(_executed_buy(), ValueError("bad")))

        with pytest.raises(BookingError):
            await strategy._initialize_grid_orders_once(current_price=15100, grid_orders_initialized=False)

        assert strategy.stop_reason is None

    def test_get_formatted_orders(self, setup_strategy):
        create_strategy, _, _, _, _, _, trading_performance_analyzer, _, _ = setup_strategy
        strategy = create_strategy()

        mock_orders = ["Order1", "Order2"]
        trading_performance_analyzer.get_formatted_orders.return_value = mock_orders

        result = strategy.get_formatted_orders()

        assert result == mock_orders
        trading_performance_analyzer.get_formatted_orders.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_take_profit_not_triggered(self, setup_strategy):
        create_strategy, _, _, _, order_manager, balance_tracker, _, _, _ = setup_strategy
        strategy = create_strategy()

        balance_tracker.crypto_balance = 1
        order_manager.execute_take_profit_or_stop_loss_order = AsyncMock()

        result = await strategy._handle_take_profit(current_price=19000)

        assert result is False
        order_manager.execute_take_profit_or_stop_loss_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_stop_loss_not_triggered(self, setup_strategy):
        create_strategy, _, _, _, order_manager, balance_tracker, _, _, _ = setup_strategy
        strategy = create_strategy()

        balance_tracker.crypto_balance = 1
        order_manager.execute_take_profit_or_stop_loss_order = AsyncMock()

        result = await strategy._handle_stop_loss(current_price=11000)

        assert result is False
        order_manager.execute_take_profit_or_stop_loss_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_take_profit_stop_loss_both_triggered(self, setup_strategy):
        create_strategy, _, _, _, order_manager, balance_tracker, _, _, event_bus = setup_strategy
        strategy = create_strategy()

        balance_tracker.crypto_balance = 1
        order_manager.execute_take_profit_or_stop_loss_order = AsyncMock()
        event_bus.publish = AsyncMock()

        result = await strategy._handle_take_profit_stop_loss(current_price=21000)

        assert result is True
        event_bus.publish.assert_called_once()
        assert event_bus.publish.await_args.args[1] == StopRequest(StopKind.RISK, "TP or SL hit.")

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_once_already_initialized(self, setup_strategy):
        create_strategy, _, _, _, order_manager, _, _, _, _ = setup_strategy
        strategy = create_strategy()

        order_manager.perform_initial_purchase = AsyncMock()
        order_manager.initialize_grid_orders = AsyncMock()

        result = await strategy._initialize_grid_orders_once(
            current_price=15100,
            grid_orders_initialized=True,
        )

        assert result is True
        order_manager.perform_initial_purchase.assert_not_called()
        order_manager.initialize_grid_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_once_waits_while_price_out_of_range(self, setup_strategy):
        create_strategy, _, _, grid_manager, order_manager, _, _, _, _ = setup_strategy
        strategy = create_strategy()

        grid_manager.is_within_grid_range.return_value = False
        order_manager.perform_initial_purchase = AsyncMock()
        order_manager.initialize_grid_orders = AsyncMock()

        result = await strategy._initialize_grid_orders_once(
            current_price=15100,
            grid_orders_initialized=False,
        )

        assert result is False
        order_manager.perform_initial_purchase.assert_not_called()
        order_manager.initialize_grid_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_once_skips_initial_purchase_on_recovery(self, setup_strategy):
        create_strategy, _, _, grid_manager, order_manager, _, _, _, _ = setup_strategy
        strategy = create_strategy()

        grid_manager.is_within_grid_range.return_value = True
        order_manager.perform_initial_purchase = AsyncMock()
        order_manager.initialize_grid_orders = AsyncMock()

        result = await strategy._initialize_grid_orders_once(
            current_price=15000,
            grid_orders_initialized=False,
            skip_initial_purchase=True,
        )

        assert result is True
        order_manager.perform_initial_purchase.assert_not_called()
        order_manager.initialize_grid_orders.assert_called_once_with(15000)


class TestTrailingStopOrchestration:
    async def test_trailing_stop_with_stop_trigger_liquidates_and_stops(self, strategy_fixture):
        s = strategy_fixture
        use_trailing_stop(s, on_trigger=TrailingTrigger.STOP)
        s.balance_tracker.crypto_balance = 1.0
        s.trailing_stop = TrailingStopLoss(atr_multiplier=1.0)
        s.trailing_stop.update(close=100.0, atr=5.0)  # stop = 95

        stopped = await s._handle_trailing_stop(current_price=94.0, atr=5.0)

        assert stopped is True
        s.order_manager.execute_take_profit_or_stop_loss_order.assert_awaited_once_with(
            current_price=94.0,
            stop_loss_order=True,
        )
        assert s.event_bus.publish.await_args.args[1] == StopRequest(StopKind.RISK, "Trailing stop hit.")

    async def test_a_regrid_trailing_trigger_does_not_liquidate(self, strategy_fixture, valid_config):
        s = strategy_fixture
        s.config_manager = config_manager_from(
            valid_config,
            {
                "grid_strategy": {"spacing": "arithmetic", "dynamic_spacing": {"enabled": True}},
                "risk_management": {"trailing_stop_loss": {"enabled": True, "on_trigger": "regrid"}},
            },
        )
        s.balance_tracker.crypto_balance = 1.0
        s.order_manager.regrid = _order_manager_regrid()
        s.trailing_stop = TrailingStopLoss(atr_multiplier=1.0)
        s.trailing_stop.update(close=100.0, atr=5.0)  # stop = 95

        stopped = await s._handle_trailing_stop(current_price=94.0, atr=5.0)

        assert stopped is False
        assert s.stop_reason is None
        assert s.trailing_stop.stop_price is None  # reset after the regrid, not liquidated
        s.order_manager.execute_take_profit_or_stop_loss_order.assert_not_awaited()

    async def test_trailing_stop_with_regrid_trigger_rebuilds_grid(self, strategy_fixture):
        s = strategy_fixture
        use_trailing_stop(s, on_trigger=TrailingTrigger.REGRID)
        use_dynamic_spacing(s)
        s.balance_tracker.crypto_balance = 1.0
        s.order_manager.regrid = _order_manager_regrid()
        s.trailing_stop = TrailingStopLoss(atr_multiplier=1.0)
        s.trailing_stop.update(close=100.0, atr=5.0)  # stop = 95

        stopped = await s._handle_trailing_stop(current_price=94.0, atr=5.0)

        assert stopped is False
        assert s.order_manager.regrid.await_args.args[0] == 94.0
        s.grid_manager.regrid.assert_called_once_with(94.0, 5.0)
        assert s.trailing_stop.stop_price is None  # reset after regrid

    async def test_no_trailing_check_without_crypto(self, strategy_fixture):
        s = strategy_fixture
        use_trailing_stop(s)
        s.balance_tracker.crypto_balance = 0
        s.balance_tracker.get_adjusted_crypto_balance.return_value = 0
        s.trailing_stop = TrailingStopLoss(atr_multiplier=1.0)

        stopped = await s._handle_trailing_stop(current_price=1.0, atr=5.0)

        assert stopped is False
        assert s.trailing_stop.stop_price is None  # not even updated

    async def test_trailing_stop_fires_when_all_crypto_is_reserved(self, strategy_fixture):
        s = strategy_fixture
        use_trailing_stop(s, on_trigger=TrailingTrigger.STOP)
        s.balance_tracker.crypto_balance = 0
        s.balance_tracker.get_adjusted_crypto_balance.return_value = 1.0
        s.trailing_stop = TrailingStopLoss(atr_multiplier=1.0)
        s.trailing_stop.update(close=100.0, atr=5.0)  # stop = 95

        stopped = await s._handle_trailing_stop(current_price=94.0, atr=5.0)

        assert stopped is True

    async def test_nan_atr_does_not_trigger_restored_stop(self, strategy_fixture):
        """After a restart, a restored stop must not fire while ATR is still NaN (pre-first-candle)."""
        s = strategy_fixture
        use_trailing_stop(s, on_trigger=TrailingTrigger.REGRID)
        s.balance_tracker.crypto_balance = 1.0
        s.order_manager.regrid = AsyncMock()
        s.trailing_stop = TrailingStopLoss(atr_multiplier=1.0)
        s.trailing_stop.stop_price = 95.0  # restored from persisted state

        stopped = await s._handle_trailing_stop(current_price=94.0, atr=float("nan"))

        assert stopped is False
        s.order_manager.regrid.assert_not_awaited()
        s.grid_manager.regrid.assert_not_called()
        assert s.trailing_stop.stop_price == 95.0  # restored protection preserved

    async def test_failed_regrid_does_not_reset_ratchet(self, strategy_fixture):
        """When the regrid fails (cancel failure), the trailing ratchet must not be reset."""
        s = strategy_fixture
        use_trailing_stop(s, on_trigger=TrailingTrigger.REGRID)
        s.balance_tracker.crypto_balance = 1.0
        s.order_manager.regrid = _order_manager_regrid(rebuilt=False)
        s.trailing_stop = TrailingStopLoss(atr_multiplier=1.0)
        s.trailing_stop.update(close=100.0, atr=5.0)  # stop = 95

        stopped = await s._handle_trailing_stop(current_price=94.0, atr=5.0)

        assert stopped is False
        s.grid_manager.regrid.assert_not_called()
        assert s.trailing_stop.stop_price == 95.0  # ratchet preserved, protection not discarded


class TestStrategyStateExportRestore:
    def test_export_includes_trailing_stop_atr_grid_and_geometry(self, strategy_fixture):
        s = strategy_fixture
        s.trailing_stop = TrailingStopLoss(atr_multiplier=2.0)
        s.trailing_stop.stop_price = 94.0
        s.grid_manager.atr_grid = 3.1
        s.grid_manager.is_initialized = True
        s.grid_manager.price_grids = [96.0, 98.0, 100.0, 102.0, 104.0]
        s.grid_manager.central_price = 100.0

        state = s.export_strategy_state()

        assert state == {
            "trailing_stop": {"stop_price": 94.0, "atr_multiplier": 2.0},
            "atr_grid": 3.1,
            "price_grids": [96.0, 98.0, 100.0, 102.0, 104.0],
            "central_price": 100.0,
        }

    def test_export_omits_geometry_when_grid_not_initialized(self, strategy_fixture):
        s = strategy_fixture
        s.trailing_stop = None
        s.grid_manager.atr_grid = None
        s.grid_manager.is_initialized = False

        state = s.export_strategy_state()

        assert state == {
            "trailing_stop": None,
            "atr_grid": None,
            "price_grids": None,
            "central_price": None,
        }

    def test_restore_sets_trailing_stop_when_enabled(self, strategy_fixture):
        s = strategy_fixture
        use_trailing_stop(s)
        s.trailing_stop = None

        s.restore_strategy_state({"trailing_stop": {"stop_price": 94.0, "atr_multiplier": 2.0}, "atr_grid": None})

        assert isinstance(s.trailing_stop, TrailingStopLoss)
        assert s.trailing_stop.stop_price == 94.0
        # config wins over the persisted atr_multiplier (fixture default is 2.5)
        assert s.trailing_stop.atr_multiplier == 2.5

    def test_restore_strategy_state_prefers_config_atr_multiplier(self, strategy_fixture):
        s = strategy_fixture
        use_trailing_stop(s, atr_multiplier=3.0)

        s.restore_strategy_state({"trailing_stop": {"stop_price": 90.0, "atr_multiplier": 2.0}, "atr_grid": None})

        assert s.trailing_stop.atr_multiplier == 3.0
        assert s.trailing_stop.stop_price == 90.0

    def test_restore_skips_trailing_stop_when_disabled(self, strategy_fixture):
        s = strategy_fixture
        s.trailing_stop = None

        s.restore_strategy_state({"trailing_stop": {"stop_price": 94.0, "atr_multiplier": 2.0}, "atr_grid": None})

        assert s.trailing_stop is None

    def test_restore_sets_atr_grid_when_present(self, strategy_fixture):
        s = strategy_fixture
        s.grid_manager.atr_grid = None

        s.restore_strategy_state({"trailing_stop": None, "atr_grid": 3.1})

        assert s.grid_manager.atr_grid == 3.1

    def test_restore_leaves_atr_grid_when_absent(self, strategy_fixture):
        s = strategy_fixture
        s.grid_manager.atr_grid = 5.0

        s.restore_strategy_state({"trailing_stop": None, "atr_grid": None})

        assert s.grid_manager.atr_grid == 5.0


class TestVolatilityRegrid:
    def _setup_dynamic(self, s):
        use_dynamic_spacing(s, regrid_threshold=0.3, cooldown_bars=5)
        s.grid_manager.is_initialized = True
        s.order_manager.regrid = _order_manager_regrid()

    async def test_first_atr_sets_baseline_without_regrid(self, strategy_fixture):
        s = strategy_fixture
        self._setup_dynamic(s)
        s.grid_manager.atr_grid = None

        await s._maybe_regrid_on_volatility(current_price=100.0, atr=2.0)

        assert s.grid_manager.atr_grid == 2.0
        s.grid_manager.regrid.assert_not_called()

    async def test_regrid_when_atr_deviates_beyond_threshold_after_cooldown(self, strategy_fixture):
        s = strategy_fixture
        self._setup_dynamic(s)
        s.grid_manager.atr_grid = 2.0
        s._bars_since_regrid = 10  # past cooldown of 5

        await s._maybe_regrid_on_volatility(current_price=100.0, atr=3.0)  # |3/2 - 1| = 0.5 > 0.3

        s.order_manager.regrid.assert_awaited_once()
        s.grid_manager.regrid.assert_called_once_with(100.0, 3.0)

    async def test_no_regrid_during_cooldown(self, strategy_fixture):
        s = strategy_fixture
        self._setup_dynamic(s)
        s.grid_manager.atr_grid = 2.0
        s._bars_since_regrid = 2  # below cooldown of 5

        await s._maybe_regrid_on_volatility(current_price=100.0, atr=3.0)

        s.grid_manager.regrid.assert_not_called()

    async def test_no_regrid_within_threshold(self, strategy_fixture):
        s = strategy_fixture
        self._setup_dynamic(s)
        s.grid_manager.atr_grid = 2.0
        s._bars_since_regrid = 10

        await s._maybe_regrid_on_volatility(current_price=100.0, atr=2.2)  # |1.1 - 1| = 0.1 < 0.3

        s.grid_manager.regrid.assert_not_called()

    async def test_cancel_failure_aborts_regrid(self, strategy_fixture):
        s = strategy_fixture
        self._setup_dynamic(s)
        s.grid_manager.atr_grid = 2.0
        s._bars_since_regrid = 10
        s.order_manager.regrid = _order_manager_regrid(rebuilt=False)

        await s._maybe_regrid_on_volatility(current_price=100.0, atr=3.0)

        s.grid_manager.regrid.assert_not_called()
        assert s._bars_since_regrid == 0  # failed attempt restarts cooldown


class TestGridFeasibilityOrchestration:
    async def test_grid_feasibility_error_stops_bot(self, strategy_fixture):
        strategy = strategy_fixture
        strategy.grid_manager.is_within_grid_range.return_value = True
        strategy.order_manager.perform_initial_purchase = AsyncMock()
        strategy.order_manager.validate_grid_feasibility = Mock(
            side_effect=GridFeasibilityError("num_grids too high"),
        )

        result = await strategy._initialize_grid_orders_once(100.0, False)

        assert result is False
        request = strategy.event_bus.publish.await_args.args[1]
        assert strategy.event_bus.publish.await_args.args[0] == Events.STOP_BOT
        assert request.kind is StopKind.INFEASIBLE
        assert "num_grids too high" in request.reason
        strategy.order_manager.perform_initial_purchase.assert_not_awaited()

    async def test_grid_feasibility_ok_proceeds_to_initial_purchase(self, strategy_fixture):
        strategy = strategy_fixture
        strategy.grid_manager.is_within_grid_range.return_value = True
        strategy.order_manager.perform_initial_purchase = AsyncMock()
        strategy.order_manager.validate_grid_feasibility = Mock()

        result = await strategy._initialize_grid_orders_once(100.0, False)

        assert result is True
        strategy.order_manager.perform_initial_purchase.assert_awaited_once_with(100.0)
