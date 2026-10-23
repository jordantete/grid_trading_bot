import logging
from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest
from tests.fakes.config import bot_config_from

from grid_trading_bot.core.order_handling.balance_tracker import AccountSnapshot
from grid_trading_bot.core.services.exceptions import DataFetchError


class TestBacktestGridStrategy:
    @pytest.mark.asyncio
    async def test_run_backtest(self, setup_strategy):
        create_strategy, _, _, grid_manager, order_manager, balance_tracker, *_ = setup_strategy
        strategy = create_strategy()

        mock_data = pd.DataFrame(
            {
                "close": [10000, 10500, 11000],
                "high": [10100, 10600, 11100],
                "low": [9900, 10400, 10900],
            },
            index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
        )
        strategy._initialize_historical_data = AsyncMock(return_value=mock_data)

        balance_tracker.get_total_balance_value.side_effect = [10000, 9500, 10000, 10000]
        balance_tracker.get_adjusted_crypto_balance.return_value = 0.5
        balance_tracker.crypto_balance = 1
        grid_manager.get_trigger_price.return_value = 8900
        order_manager.initialize_grid_orders = AsyncMock()
        strategy._initialize_grid_orders_once = AsyncMock(side_effect=[False, True, True])
        strategy._handle_take_profit_stop_loss = AsyncMock(side_effect=[False, False, False])

        await strategy.run()

        expected_account_values = pd.Series([9500, 10000, 10000], index=strategy.data.index, name="account_value")
        pd.testing.assert_series_equal(strategy.data["account_value"], expected_account_values.astype("float64"))
        expected_crypto_values = pd.Series([5000, 5250, 5500], index=strategy.data.index, name="crypto_value")
        pd.testing.assert_series_equal(strategy.data["crypto_value"], expected_crypto_values.astype("float64"))
        assert strategy._starting_account_value == 10000
        strategy._handle_take_profit_stop_loss.assert_awaited()

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
        strategy._starting_account_value = 15000
        trading_performance_analyzer.generate_performance_summary = Mock()
        balance_tracker.snapshot.return_value = AccountSnapshot(
            price=11000.0, value=16000.0, peak=16000.0, drawdown_pct=0.0, realized_pnl=42.0, unrealized_pnl=-3.0
        )

        strategy.generate_performance_report()

        balance_tracker.snapshot.assert_called_once_with(final_price)
        trading_performance_analyzer.generate_performance_summary.assert_called_once_with(
            strategy.data,
            initial_price,
            balance_tracker.get_adjusted_fiat_balance(),
            balance_tracker.get_adjusted_crypto_balance(),
            final_price,
            balance_tracker.total_fees,
            42.0,
            -3.0,
            initial_balance=15000,
            armed=False,
            stop_reason=None,
        )

    def test_plot_results(self, setup_strategy):
        create_strategy, _, _, _, _, _, _, plotter, _ = setup_strategy
        strategy = create_strategy()
        strategy.data = pd.DataFrame({"close": [10000, 10500, 11000]})

        strategy.plot_results()

        plotter.plot_results.assert_called_once_with(strategy.data)

    @pytest.mark.asyncio
    async def test_initialize_historical_data_backtest_mode(self, setup_strategy, valid_config):
        create_strategy, config_manager, exchange_service, _, _, _, _, _, _ = setup_strategy

        config_manager.config = bot_config_from(
            valid_config,
            {"trading_settings": {"timeframe": "1h", "period": {"start_date": "2024-01-01", "end_date": "2024-01-02"}}},
        )

        mock_data = pd.DataFrame({"close": [100, 200, 300]})
        exchange_service.fetch_ohlcv.return_value = mock_data

        strategy = create_strategy()
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
        strategy = create_strategy()

        # Mock the exchange service to raise an exception
        exchange_service.fetch_ohlcv.side_effect = DataFetchError("Failed to fetch data")

        with caplog.at_level(logging.ERROR), pytest.raises(DataFetchError, match="Failed to fetch data"):
            await strategy._initialize_historical_data()

        assert "Failed to initialize data for backtest trading mode" in caplog.text

    @pytest.mark.asyncio
    async def test_run_backtest_with_no_data(self, setup_strategy):
        create_strategy, _, _, _, _, _, _, _, _ = setup_strategy
        strategy = create_strategy()
        strategy._initialize_historical_data = AsyncMock(return_value=None)

        await strategy.run()

        assert strategy._running is False


async def test_a_backtest_without_candles_reports_no_metrics_and_plots_nothing(strategy_fixture, caplog):
    strategy = strategy_fixture
    strategy.exchange_service.fetch_ohlcv.side_effect = DataFetchError("no candles")

    with pytest.raises(DataFetchError, match="no candles"):
        await strategy.run()

    assert strategy.generate_performance_report() == (None, {}, [])
    caplog.clear()
    strategy.plot_results()
    assert "No backtest data to plot." in [record.getMessage() for record in caplog.records]
    strategy.plotter.plot_results.assert_not_called()
