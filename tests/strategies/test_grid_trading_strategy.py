import logging
from unittest.mock import AsyncMock, Mock

import numpy as np
import pandas as pd
import pytest

from config.config_manager import ConfigManager
from config.trading_mode import TradingMode
from core.bot_management.event_bus import EventBus
from core.grid_management.grid_manager import GridManager
from core.order_handling.balance_tracker import BalanceTracker
from core.order_handling.order_manager import OrderManager
from core.order_handling.order_simulator import OrderSimulator
from core.services.exchange_interface import ExchangeInterface
from strategies.grid_trading_strategy import GridTradingStrategy
from strategies.plotter import Plotter
from strategies.trading_performance_analyzer import TradingPerformanceAnalyzer


class TestGridTradingStrategy:
    @pytest.fixture
    def setup_strategy(self):
        config_manager = Mock(spec=ConfigManager)
        exchange_service = Mock(spec=ExchangeInterface)
        grid_manager = Mock(spec=GridManager)
        order_manager = Mock(spec=OrderManager)
        balance_tracker = Mock(spec=BalanceTracker)
        trading_performance_analyzer = Mock(spec=TradingPerformanceAnalyzer)
        plotter = Mock(spec=Plotter)
        event_bus = Mock(spec=EventBus)
        order_simulator = Mock(spec=OrderSimulator)
        order_simulator.simulate_order_fills = AsyncMock()

        config_manager.get_timeframe.return_value = "1d"
        config_manager.is_take_profit_enabled.return_value = True
        config_manager.is_stop_loss_enabled.return_value = True
        config_manager.get_take_profit_threshold.return_value = 20000
        config_manager.get_stop_loss_threshold.return_value = 10000

        def create_strategy(trading_mode: TradingMode = TradingMode.BACKTEST):
            return GridTradingStrategy(
                config_manager=config_manager,
                event_bus=event_bus,
                exchange_service=exchange_service,
                grid_manager=grid_manager,
                order_manager=order_manager,
                balance_tracker=balance_tracker,
                trading_performance_analyzer=trading_performance_analyzer,
                trading_mode=trading_mode,
                trading_pair="BTC/USDT",
                plotter=plotter,
                order_simulator=order_simulator,
            )

        return (
            create_strategy,
            config_manager,
            exchange_service,
            grid_manager,
            order_manager,
            balance_tracker,
            trading_performance_analyzer,
            plotter,
            event_bus,
        )

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
    async def test_restart_live_trading(self, setup_strategy):
        create_strategy, _, exchange_service, grid_manager, _, _, _, _, _ = setup_strategy
        strategy = create_strategy(TradingMode.LIVE)
        grid_manager.get_trigger_price.return_value = 10500
        exchange_service.listen_to_ticker_updates = AsyncMock()
        strategy._running = False

        await strategy.restart()

        # Assert that the strategy started and `listen_to_ticker_updates` was called
        assert strategy._running is True, "Expected strategy to be running after restart in LIVE mode."

        # Extract the actual callback passed to `listen_to_ticker_updates`
        actual_call_args = exchange_service.listen_to_ticker_updates.call_args
        actual_callback = actual_call_args[0][1]  # Extract the callback argument

        # Verify `listen_to_ticker_updates` was called with the correct parameters
        assert actual_call_args[0][0] == strategy.trading_pair, "Expected the trading pair to be passed."
        assert (
            actual_call_args[0][2] == strategy.TICKER_REFRESH_INTERVAL
        ), "Expected the correct ticker refresh interval."
        assert callable(actual_callback), "Expected a callable callback for on_ticker_update."

    @pytest.mark.asyncio
    async def test_run_backtest(self, setup_strategy):
        create_strategy, _, _, grid_manager, order_manager, balance_tracker, *_ = setup_strategy
        strategy = create_strategy()

        mock_data = pd.DataFrame(
            {
                "close": [10000, 10500, 11000],
                "high": [10100, 10600, 11100],
                "low": [9900, 10400, 10900],
                "account_value": [np.nan, np.nan, np.nan],
            },
            index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
        )
        strategy._initialize_historical_data = AsyncMock(return_value=mock_data)

        balance_tracker.get_total_balance_value.side_effect = [9000, 9500, 10000, 10000]
        balance_tracker.crypto_balance = 1
        grid_manager.get_trigger_price.return_value = 8900
        order_manager.initialize_grid_orders = AsyncMock()
        strategy._initialize_grid_orders_once = AsyncMock(side_effect=[False, True, True])
        strategy._handle_take_profit_stop_loss = AsyncMock(side_effect=[False, False, False])

        await strategy.run()

        expected_account_values = pd.Series([9500, 10000, 10000], index=strategy.data.index, name="account_value")
        pd.testing.assert_series_equal(strategy.data["account_value"], expected_account_values.astype("float64"))
        strategy._handle_take_profit_stop_loss.assert_awaited()

    @pytest.mark.asyncio
    async def test_run_live_trading(self, setup_strategy):
        create_strategy, _, exchange_service, *_ = setup_strategy
        strategy = create_strategy(TradingMode.LIVE)
        exchange_service.listen_to_ticker_updates = AsyncMock()

        await strategy.run()

        exchange_service.listen_to_ticker_updates.assert_called_once()

    def test_generate_performance_report(self, setup_strategy):
        create_strategy, _, _, _, _, balance_tracker, trading_performance_analyzer, _, _ = setup_strategy
        strategy = create_strategy()
        strategy.data = pd.DataFrame({"close": [10000, 10500, 11000]})
        strategy.close_prices = strategy.data["close"].values
        initial_price = strategy.data["close"].iloc[0]
        final_price = strategy.data["close"].iloc[-1]
        balance_tracker.get_adjusted_fiat_balance.return_value = 5000
        balance_tracker.get_adjusted_crypto_balance.return_value = 1
        balance_tracker.total_fees = 10
        trading_performance_analyzer.generate_performance_summary = Mock()

        strategy.generate_performance_report()

        trading_performance_analyzer.generate_performance_summary.assert_called_once_with(
            strategy.data,
            initial_price,
            balance_tracker.get_adjusted_fiat_balance(),
            balance_tracker.get_adjusted_crypto_balance(),
            final_price,
            balance_tracker.total_fees,
        )

    def test_plot_results(self, setup_strategy):
        create_strategy, _, _, _, _, _, _, plotter, _ = setup_strategy
        strategy = create_strategy()
        strategy.data = pd.DataFrame({"close": [10000, 10500, 11000]})

        strategy.plot_results()

        plotter.plot_results.assert_called_once_with(strategy.data)

    def test_plot_results_not_available_in_live_mode(self, setup_strategy, caplog):
        create_strategy, _, _, _, _, _, _, plotter, _ = setup_strategy
        strategy = create_strategy(TradingMode.LIVE)

        with caplog.at_level(logging.INFO):
            strategy.plot_results()

        assert "Plotting is not available for live/paper trading mode." in [record.message for record in caplog.records]
        plotter.plot_results.assert_not_called()

    @pytest.mark.asyncio
    async def test_initialize_historical_data_live_mode(self, setup_strategy):
        create_strategy, _, _, _, _, _, _, _, _ = setup_strategy
        strategy = create_strategy(TradingMode.LIVE)

        result = await strategy._initialize_historical_data()
        assert result is None

    @pytest.mark.asyncio
    async def test_initialize_historical_data_backtest_mode(self, setup_strategy):
        create_strategy, config_manager, exchange_service, _, _, _, _, _, _ = setup_strategy

        config_manager.get_timeframe.return_value = "1h"
        config_manager.get_start_date.return_value = "2024-01-01"
        config_manager.get_end_date.return_value = "2024-01-02"

        mock_data = pd.DataFrame({"close": [100, 200, 300]})
        exchange_service.fetch_ohlcv.return_value = mock_data

        strategy = create_strategy(TradingMode.BACKTEST)
        exchange_service.fetch_ohlcv.reset_mock()

        result = await strategy._initialize_historical_data()

        assert result is not None
        assert isinstance(result, pd.DataFrame)
        exchange_service.fetch_ohlcv.assert_called_once_with(
            "BTC/USDT",
            "1h",
            "2024-01-01",
            "2024-01-02",
        )

    @pytest.mark.asyncio
    async def test_initialize_historical_data_error(self, setup_strategy, caplog):
        create_strategy, _, exchange_service, _, _, _, _, _, _ = setup_strategy
        strategy = create_strategy(TradingMode.BACKTEST)

        # Mock the exchange service to raise an exception
        exchange_service.fetch_ohlcv.side_effect = Exception("Failed to fetch data")

        with caplog.at_level(logging.ERROR):
            result = await strategy._initialize_historical_data()

        assert result is None
        assert "Failed to initialize data for backtest trading mode" in caplog.text

    @pytest.mark.asyncio
    async def test_evaluate_tp_or_sl_no_crypto_balance(self, setup_strategy):
        create_strategy, _, _, _, _, balance_tracker, _, _, _ = setup_strategy
        strategy = create_strategy()

        balance_tracker.crypto_balance = 0

        result = await strategy._evaluate_tp_or_sl(current_price=15000)
        assert result is False

    @pytest.mark.asyncio
    async def test_handle_take_profit_triggered(self, setup_strategy):
        create_strategy, config_manager, _, _, order_manager, balance_tracker, _, _, _ = setup_strategy
        strategy = create_strategy()

        config_manager.is_take_profit_enabled.return_value = True
        config_manager.get_take_profit_threshold.return_value = 20000
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
        create_strategy, config_manager, _, _, order_manager, balance_tracker, _, _, _ = setup_strategy
        strategy = create_strategy()

        config_manager.is_stop_loss_enabled.return_value = True
        config_manager.get_stop_loss_threshold.return_value = 10000
        balance_tracker.crypto_balance = 1
        order_manager.execute_take_profit_or_stop_loss_order = AsyncMock()

        result = await strategy._handle_stop_loss(current_price=9000)

        assert result is True
        order_manager.execute_take_profit_or_stop_loss_order.assert_called_once_with(
            current_price=9000,
            stop_loss_order=True,
        )

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_once_first_time(self, setup_strategy):
        create_strategy, _, _, grid_manager, order_manager, _, _, _, _ = setup_strategy
        strategy = create_strategy()

        grid_manager.get_trigger_price.return_value = 15000
        order_manager.perform_initial_purchase = AsyncMock()
        order_manager.initialize_grid_orders = AsyncMock()

        result = await strategy._initialize_grid_orders_once(
            current_price=15100,
            trigger_price=15000,
            grid_orders_initialized=False,
            last_price=14900,
        )

        assert result is True
        order_manager.perform_initial_purchase.assert_called_once_with(15100)
        order_manager.initialize_grid_orders.assert_called_once_with(15100)

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
        create_strategy, config_manager, _, _, order_manager, balance_tracker, _, _, _ = setup_strategy
        strategy = create_strategy()

        config_manager.is_take_profit_enabled.return_value = True
        config_manager.get_take_profit_threshold.return_value = 20000
        balance_tracker.crypto_balance = 1
        order_manager.execute_take_profit_or_stop_loss_order = AsyncMock()

        result = await strategy._handle_take_profit(current_price=19000)

        assert result is False
        order_manager.execute_take_profit_or_stop_loss_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_stop_loss_not_triggered(self, setup_strategy):
        create_strategy, config_manager, _, _, order_manager, balance_tracker, _, _, _ = setup_strategy
        strategy = create_strategy()

        config_manager.is_stop_loss_enabled.return_value = True
        config_manager.get_stop_loss_threshold.return_value = 10000
        balance_tracker.crypto_balance = 1
        order_manager.execute_take_profit_or_stop_loss_order = AsyncMock()

        result = await strategy._handle_stop_loss(current_price=11000)

        assert result is False
        order_manager.execute_take_profit_or_stop_loss_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_take_profit_stop_loss_both_triggered(self, setup_strategy):
        create_strategy, config_manager, _, _, order_manager, balance_tracker, _, _, event_bus = setup_strategy
        strategy = create_strategy()

        config_manager.is_take_profit_enabled.return_value = True
        config_manager.get_take_profit_threshold.return_value = 20000
        balance_tracker.crypto_balance = 1
        order_manager.execute_take_profit_or_stop_loss_order = AsyncMock()
        event_bus.publish = AsyncMock()

        result = await strategy._handle_take_profit_stop_loss(current_price=21000)

        assert result is True
        event_bus.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_once_already_initialized(self, setup_strategy):
        create_strategy, _, _, _, order_manager, _, _, _, _ = setup_strategy
        strategy = create_strategy()

        order_manager.perform_initial_purchase = AsyncMock()
        order_manager.initialize_grid_orders = AsyncMock()

        result = await strategy._initialize_grid_orders_once(
            current_price=15100,
            trigger_price=15000,
            grid_orders_initialized=True,
            last_price=14900,
        )

        assert result is True
        order_manager.perform_initial_purchase.assert_not_called()
        order_manager.initialize_grid_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_once_no_last_price(self, setup_strategy):
        create_strategy, _, _, _, order_manager, _, _, _, _ = setup_strategy
        strategy = create_strategy()

        order_manager.perform_initial_purchase = AsyncMock()
        order_manager.initialize_grid_orders = AsyncMock()

        result = await strategy._initialize_grid_orders_once(
            current_price=15100,
            trigger_price=15000,
            grid_orders_initialized=False,
            last_price=None,
        )

        assert result is False
        order_manager.perform_initial_purchase.assert_not_called()
        order_manager.initialize_grid_orders.assert_not_called()

    def test_generate_performance_report_live_mode(self, setup_strategy):
        create_strategy, _, _, _, _, balance_tracker, trading_performance_analyzer, _, _ = setup_strategy
        strategy = create_strategy(TradingMode.LIVE)

        balance_tracker.get_adjusted_fiat_balance.return_value = 5000
        balance_tracker.get_adjusted_crypto_balance.return_value = 1
        balance_tracker.total_fees = 10

        strategy.live_trading_metrics = [
            (pd.Timestamp("2024-01-01"), 10000, 100),
            (pd.Timestamp("2024-01-02"), 11000, 110),
        ]

        strategy.generate_performance_report()

        trading_performance_analyzer.generate_performance_summary.assert_called_once()

    def test_generate_performance_report_live_mode_no_metrics(self, setup_strategy):
        create_strategy, _, _, _, _, _, trading_performance_analyzer, _, _ = setup_strategy
        strategy = create_strategy(TradingMode.LIVE)

        result, formatted_orders = strategy.generate_performance_report()

        assert result == {}
        assert formatted_orders == []
        trading_performance_analyzer.generate_performance_summary.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_live_trading_error_handling(self, setup_strategy):
        create_strategy, _, exchange_service, _, _, _, _, _, _ = setup_strategy
        strategy = create_strategy(TradingMode.LIVE)

        exchange_service.listen_to_ticker_updates = AsyncMock(side_effect=Exception("Connection error"))

        await strategy.run()

        exchange_service.listen_to_ticker_updates.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_backtest_with_no_data(self, setup_strategy):
        create_strategy, _, _, _, _, _, _, _, _ = setup_strategy
        strategy = create_strategy(TradingMode.BACKTEST)
        strategy._initialize_historical_data = AsyncMock(return_value=None)

        await strategy.run()

        assert strategy._running is False

    @pytest.mark.asyncio
    async def test_initialize_grid_orders_once_trigger_price_equals_last_price(self, setup_strategy):
        create_strategy, _, _, _, order_manager, _, _, _, _ = setup_strategy
        strategy = create_strategy()

        order_manager.perform_initial_purchase = AsyncMock()
        order_manager.initialize_grid_orders = AsyncMock()

        result = await strategy._initialize_grid_orders_once(
            current_price=15000,
            trigger_price=15000,
            grid_orders_initialized=False,
            last_price=15000,
        )

        assert result is True
        order_manager.perform_initial_purchase.assert_called_once_with(15000)
        order_manager.initialize_grid_orders.assert_called_once_with(15000)

    @pytest.mark.asyncio
    async def test_run_live_trading_stop_condition(self, setup_strategy):
        create_strategy, _, exchange_service, _, _, _, _, _, _ = setup_strategy
        strategy = create_strategy(TradingMode.LIVE)

        async def stop_strategy(*args, **kwargs):
            strategy._running = False

        exchange_service.listen_to_ticker_updates = AsyncMock(side_effect=stop_strategy)

        await strategy.run()

        assert not strategy._running
        exchange_service.listen_to_ticker_updates.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_ticker_update_error_handling(self, setup_strategy):
        create_strategy, _, exchange_service, grid_manager, order_manager, balance_tracker, _, _, _ = setup_strategy
        strategy = create_strategy(TradingMode.LIVE)

        grid_manager.get_trigger_price.return_value = 15000
        balance_tracker.get_total_balance_value.side_effect = Exception("Balance calculation error")

        async def simulate_ticker_update():
            callback = exchange_service.listen_to_ticker_updates.call_args[0][1]
            await callback(15100)

        exchange_service.listen_to_ticker_updates = AsyncMock(side_effect=simulate_ticker_update)

        await strategy.run()

        exchange_service.listen_to_ticker_updates.assert_called_once()
