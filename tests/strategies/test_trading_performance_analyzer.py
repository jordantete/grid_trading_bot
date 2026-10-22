import dataclasses
import logging
import math
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest
from tests.fakes.config import config_manager_from

from grid_trading_bot.core.grid_management.grid_level import GridLevel
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus, OrderType
from grid_trading_bot.strategies.trading_performance_analyzer import ANNUAL_RISK_FREE_RATE, TradingPerformanceAnalyzer


def _use_timeframe(config_manager, timeframe):
    settings = dataclasses.replace(config_manager.config.trading_settings, timeframe=timeframe)
    config_manager.config = dataclasses.replace(config_manager.config, trading_settings=settings)


class TestPerformanceAnalyzer:
    @pytest.fixture
    def setup_performance_analyzer(self, valid_config):
        config_manager = config_manager_from(
            valid_config,
            {
                "trading_settings": {"initial_balance": 10000, "timeframe": "1d"},
                "pair": {"base_currency": "BTC", "quote_currency": "USDT"},
                "exchange": {"trading_fee": 0.001},
            },
        )
        order_book = Mock()
        analyzer = TradingPerformanceAnalyzer(config_manager, order_book)
        return analyzer, config_manager, order_book

    @pytest.fixture
    def mock_account_data(self):
        data = pd.DataFrame(
            {
                "close": [100, 105, 110, 90, 95],
                "account_value": [10000, 10250, 10500, 9500, 9800],
            },
            index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]),
        )
        return data

    def test_calculate_roi(self, setup_performance_analyzer):
        analyzer, _, _ = setup_performance_analyzer
        roi = analyzer._calculate_roi(10000, 11000)
        assert roi == 10.0  # Expected 10% ROI given a 10000 initial balance

    def test_calculate_roi_zero_balance(self, setup_performance_analyzer):
        analyzer, _, _ = setup_performance_analyzer
        roi = analyzer._calculate_roi(10000, 10000)
        assert roi == 0.0  # Expected 0% ROI when final balance matches initial balance

    def test_roi_is_kept_at_full_precision(self, setup_performance_analyzer):
        analyzer, _, _ = setup_performance_analyzer

        assert analyzer._calculate_roi(150, 150.1006633) == pytest.approx(0.06710886666667193, rel=1e-12)

    def test_sharpe_is_kept_at_full_precision(self, setup_performance_analyzer):
        analyzer, _, _ = setup_performance_analyzer
        data = pd.DataFrame(
            {"account_value": [10000, 10100, 10050, 10200, 10150, 10300]},
            index=pd.date_range("2024-01-01", periods=6, freq="1D"),
        )
        periods = analyzer._periods_per_year()
        excess = data["account_value"].pct_change() - ANNUAL_RISK_FREE_RATE / periods
        expected = excess.mean() / excess.std() * np.sqrt(periods)

        sharpe = analyzer._calculate_sharpe_ratio(data)

        assert sharpe == pytest.approx(expected, rel=1e-12)
        assert sharpe != round(sharpe, 2)

    def test_sortino_is_kept_at_full_precision(self, setup_performance_analyzer):
        analyzer, _, _ = setup_performance_analyzer
        data = pd.DataFrame(
            {"account_value": [10000, 10100, 10050, 10200, 10150, 10300]},
            index=pd.date_range("2024-01-01", periods=6, freq="1D"),
        )
        periods = analyzer._periods_per_year()
        excess = data["account_value"].pct_change() - ANNUAL_RISK_FREE_RATE / periods
        expected = excess.mean() / excess[excess < 0].std() * np.sqrt(periods)

        sortino = analyzer._calculate_sortino_ratio(data)

        assert sortino == pytest.approx(expected, rel=1e-12)
        assert sortino != round(sortino, 2)

    def test_a_sortino_without_returns_stays_nan_and_displays_as_before(self, setup_performance_analyzer):
        analyzer, _, _ = setup_performance_analyzer
        data = pd.DataFrame({"account_value": [10000]}, index=pd.date_range("2024-01-01", periods=1, freq="1D"))

        sortino = analyzer._calculate_sortino_ratio(data)

        assert math.isnan(sortino)
        assert f"{sortino:.2f}" == "nan"

    def test_calculate_drawdown(self, setup_performance_analyzer, mock_account_data):
        analyzer, _, _ = setup_performance_analyzer
        max_drawdown = analyzer._calculate_drawdown(mock_account_data)
        assert max_drawdown == pytest.approx(9.52, rel=1e-3)

    def test_calculate_runup(self, setup_performance_analyzer, mock_account_data):
        analyzer, _, _ = setup_performance_analyzer
        max_runup = analyzer._calculate_runup(mock_account_data)
        assert max_runup == 5.0  # Expected max runup from 10000 to 10500 (5%)

    @staticmethod
    def _daily(values: list[float]) -> pd.DataFrame:
        return pd.DataFrame({"account_value": values}, index=pd.date_range("2024-01-01", periods=len(values), freq="D"))

    def test_drawdown_duration_runs_from_the_peak_to_the_recovery(self, setup_performance_analyzer):
        analyzer, _, _ = setup_performance_analyzer

        duration = analyzer._calculate_max_drawdown_duration(self._daily([100, 90, 95, 100, 101]))

        assert duration == pd.Timedelta(days=3)

    def test_drawdown_still_open_runs_to_the_last_row(self, setup_performance_analyzer):
        analyzer, _, _ = setup_performance_analyzer

        duration = analyzer._calculate_max_drawdown_duration(self._daily([100, 110, 99, 105]))

        assert duration == pd.Timedelta(days=2)

    def test_drawdown_duration_keeps_the_longest_one(self, setup_performance_analyzer):
        analyzer, _, _ = setup_performance_analyzer

        duration = analyzer._calculate_max_drawdown_duration(self._daily([100, 90, 100, 95, 92, 99, 101]))

        assert duration == pd.Timedelta(days=4)

    def test_rising_or_flat_account_has_no_drawdown_duration(self, setup_performance_analyzer):
        analyzer, _, _ = setup_performance_analyzer

        assert analyzer._calculate_max_drawdown_duration(self._daily([100, 100, 100])) == pd.Timedelta(0)
        assert analyzer._calculate_max_drawdown_duration(self._daily([100, 101, 102])) == pd.Timedelta(0)

    def test_drawdown_duration_ignores_missing_account_values(self, setup_performance_analyzer):
        analyzer, _, _ = setup_performance_analyzer

        duration = analyzer._calculate_max_drawdown_duration(self._daily([100, 90, float("nan"), float("nan")]))

        assert duration == pd.Timedelta(days=1)

    def test_grid_trading_gains_are_the_realized_pnl(self, setup_performance_analyzer):
        analyzer, _, order_book = setup_performance_analyzer
        order_book.get_all_sell_orders.return_value = [Mock(spec=Order, is_filled=Mock(return_value=True))]

        assert analyzer._calculate_grid_trading_gains(-12.5) == -12.5

    def test_grid_trading_gains_not_available_before_any_sale(self, setup_performance_analyzer):
        analyzer, _, order_book = setup_performance_analyzer
        order_book.get_all_sell_orders.return_value = []

        assert analyzer._calculate_grid_trading_gains(0.0) is None

    def test_grid_trading_gains_show_a_liquidation_without_grid_sells(self, setup_performance_analyzer):
        analyzer, _, order_book = setup_performance_analyzer
        order_book.get_all_sell_orders.return_value = []

        assert analyzer._calculate_grid_trading_gains(-3.2) == -3.2

    def test_avg_crypto_exposure_is_the_mean_share_held_in_crypto(self, setup_performance_analyzer):
        analyzer, _, _ = setup_performance_analyzer
        data = pd.DataFrame(
            {"account_value": [1000.0, 1000.0, 1000.0, 1000.0], "crypto_value": [0.0, 500.0, 1000.0, 500.0]}
        )

        assert analyzer._calculate_avg_crypto_exposure(data) == pytest.approx(0.5)

    def test_avg_crypto_exposure_is_none_without_crypto_values(self, setup_performance_analyzer, mock_account_data):
        analyzer, _, _ = setup_performance_analyzer

        assert analyzer._calculate_avg_crypto_exposure(mock_account_data) is None

    def test_calculate_sharpe_ratio(self, setup_performance_analyzer, mock_account_data):
        analyzer, _, _ = setup_performance_analyzer
        sharpe_ratio = analyzer._calculate_sharpe_ratio(mock_account_data)
        assert isinstance(sharpe_ratio, float)

    def test_calculate_sharpe_ratio_no_volatility(self, setup_performance_analyzer):
        analyzer, _, _ = setup_performance_analyzer
        data = pd.DataFrame(
            {"account_value": [10000, 10000, 10000]},
            index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
        )
        sharpe_ratio = analyzer._calculate_sharpe_ratio(data)
        assert sharpe_ratio == 0.0  # Expected Sharpe ratio to be 0 when there is no volatility

    def test_calculate_sharpe_ratio_near_flat_account_value_is_zero(self, setup_performance_analyzer):
        """Float noise on a flat account value must not bypass the zero-volatility
        guard (an exact == 0 check let std ~ 1e-14 produce Sharpe values of +/-1e16)."""
        analyzer, _, _ = setup_performance_analyzer
        data = pd.DataFrame(
            {"account_value": [10000.0, 10000.00000001, 10000.0, 10000.00000001, 10000.0]},
            index=pd.date_range("2024-01-01", periods=5, freq="1D"),
        )
        sharpe_ratio = analyzer._calculate_sharpe_ratio(data)
        assert sharpe_ratio == 0.0

    def test_calculate_sharpe_ratio_annualizes_by_timeframe(self, setup_performance_analyzer):
        """The same return series sampled on 1m bars must annualize ~sqrt(1440) higher
        than on 1d bars (the old code always used the daily sqrt(252) factor)."""
        analyzer, config_manager, _ = setup_performance_analyzer
        data = pd.DataFrame(
            {"account_value": [10000, 10100, 10050, 10200, 10150, 10300]},
            index=pd.date_range("2024-01-01", periods=6, freq="1D"),
        )

        _use_timeframe(config_manager, "1d")
        sharpe_daily = analyzer._calculate_sharpe_ratio(data)

        _use_timeframe(config_manager, "1m")
        sharpe_minute = analyzer._calculate_sharpe_ratio(data)

        assert sharpe_daily != 0.0
        assert sharpe_minute == pytest.approx(sharpe_daily * (1440**0.5), rel=0.05)

    def test_calculate_sortino_ratio_annualizes_by_timeframe(self, setup_performance_analyzer):
        analyzer, config_manager, _ = setup_performance_analyzer
        data = pd.DataFrame(
            {"account_value": [10000, 10100, 10050, 10200, 10150, 10300]},
            index=pd.date_range("2024-01-01", periods=6, freq="1D"),
        )

        _use_timeframe(config_manager, "1d")
        sortino_daily = analyzer._calculate_sortino_ratio(data)

        _use_timeframe(config_manager, "1m")
        sortino_minute = analyzer._calculate_sortino_ratio(data)

        assert sortino_daily != 0.0
        assert sortino_minute == pytest.approx(sortino_daily * (1440**0.5), rel=0.05)

    def test_get_formatted_orders(self, setup_performance_analyzer):
        analyzer, _, order_book = setup_performance_analyzer

        grid_level_1 = Mock(spec=GridLevel)
        grid_level_1.price = 1000.0
        grid_level_2 = Mock(spec=GridLevel)
        grid_level_2.price = 1200.0

        buy_order = Mock(
            spec=Order,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            status=OrderStatus.CLOSED,
            price=1000.0,
            average=1005.0,
            filled=0.5,
            format_last_trade_timestamp=Mock(return_value="2024-01-01T00:00:00Z"),
            is_filled=Mock(return_value=True),
        )
        sell_order = Mock(
            spec=Order,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            status=OrderStatus.CLOSED,
            price=1200.0,
            average=1195.0,
            filled=0.4,
            format_last_trade_timestamp=Mock(return_value="2024-01-02T00:00:00Z"),
            is_filled=Mock(return_value=True),
        )

        order_book.get_buy_orders_with_grid.return_value = [(buy_order, grid_level_1)]
        order_book.get_sell_orders_with_grid.return_value = [(sell_order, grid_level_2)]

        formatted_orders = analyzer.get_formatted_orders()

        assert formatted_orders[0] == [
            "BUY",
            "LIMIT",
            "CLOSED",
            1000.0,
            0.5,
            "2024-01-01T00:00:00Z",
            1000.0,
            "0.50%",
        ]

        # Verify sell order formatting
        assert formatted_orders[1] == [
            "SELL",
            "LIMIT",
            "CLOSED",
            1200.0,
            0.4,
            "2024-01-02T00:00:00Z",
            1200.0,
            "-0.42%",
        ]

    def test_get_formatted_orders_empty(self, setup_performance_analyzer):
        analyzer, _, order_book = setup_performance_analyzer
        order_book.get_buy_orders_with_grid.return_value = []
        order_book.get_sell_orders_with_grid.return_value = []

        formatted_orders = analyzer.get_formatted_orders()
        assert formatted_orders == []

    def test_generate_performance_summary(self, setup_performance_analyzer, mock_account_data, caplog):
        analyzer, config_manager, order_book = setup_performance_analyzer

        initial_balance = mock_account_data["account_value"].iloc[0]
        initial_price = mock_account_data["close"].iloc[0]
        final_fiat_balance = 10500
        final_crypto_balance = 0.5
        final_crypto_price = 20000
        total_fees = 50

        # Mock orders and grid levels
        buy_order = Mock(
            spec=Order,
            identifier="123",
            price=1000,
            amount=1.0,
            average=1000.0,
            fee={"cost": 1.0},
            order_type=OrderType.MARKET,
            status=OrderStatus.CLOSED,
            side=OrderSide.BUY,
            filled=1,
            format_last_trade_timestamp=Mock(return_value="2024-01-01T00:00:00Z"),
            is_filled=Mock(return_value=True),
        )
        sell_order = Mock(
            spec=Order,
            identifier="321",
            price=1200,
            amount=1.0,
            average=1200,
            fee={"cost": 1.5},
            order_type=OrderType.MARKET,
            status=OrderStatus.CLOSED,
            side=OrderSide.SELL,
            filled=1,
            format_last_trade_timestamp=Mock(return_value="2024-01-02T00:00:00Z"),
            is_filled=Mock(return_value=True),
        )
        grid_level = Mock(spec=GridLevel, price=1000)

        # Mock order_book responses
        order_book.get_all_buy_orders.return_value = [buy_order]
        order_book.get_all_sell_orders.return_value = [sell_order]
        order_book.get_buy_orders_with_grid.return_value = [(buy_order, grid_level)]
        order_book.get_sell_orders_with_grid.return_value = [(sell_order, grid_level)]

        # Capture logs during the performance summary generation
        with caplog.at_level(logging.INFO):
            metrics, performance_summary, formatted_orders = analyzer.generate_performance_summary(
                mock_account_data,
                initial_price,
                final_fiat_balance,
                final_crypto_balance,
                final_crypto_price,
                total_fees,
                12.5,
                -7.25,
                initial_balance=initial_balance,
                armed=True,
                stop_reason=None,
            )

        # Assertions for performance summary
        assert (
            performance_summary["Pair"]
            == f"{config_manager.config.pair.base_currency}/{config_manager.config.pair.quote_currency}"
        )
        assert performance_summary["Start Date"] == mock_account_data.index[0]
        assert performance_summary["End Date"] == mock_account_data.index[-1]
        assert performance_summary["Duration"] == mock_account_data.index[-1] - mock_account_data.index[0]
        expected_roi = analyzer._calculate_roi(
            initial_balance,
            final_fiat_balance + final_crypto_balance * final_crypto_price,
        )
        assert performance_summary["ROI"] == f"{expected_roi:.2f}%"
        assert performance_summary["Grid Trading Gains"] == "12.50"
        assert performance_summary["Unrealized PnL"] == "-7.25"
        assert performance_summary["Max Drawdown Duration"] == pd.Timedelta(days=2)
        assert performance_summary["Total Fees"] == f"{total_fees:.2f}"
        assert (
            performance_summary["Final Balance (Fiat)"]
            == f"{final_fiat_balance + final_crypto_balance * final_crypto_price:.2f}"
        )
        assert (
            performance_summary["Final Crypto Balance"]
            == f"{final_crypto_balance:.4f} {config_manager.config.pair.base_currency}"
        )
        assert (
            performance_summary["Remaining Fiat Balance"]
            == f"{final_fiat_balance:.2f} {config_manager.config.pair.quote_currency}"
        )
        assert performance_summary["Number of Buy Trades"] == 1
        assert performance_summary["Number of Sell Trades"] == 1
        assert "Sharpe Ratio" in performance_summary
        assert "Sortino Ratio" in performance_summary
        assert performance_summary["ROI"] == f"{metrics.roi_pct:.2f}%"
        assert performance_summary["Max Drawdown"] == f"{metrics.max_drawdown_pct:.2f}%"
        assert performance_summary["Sharpe Ratio"] == f"{metrics.sharpe_ratio:.2f}"
        assert performance_summary["Sortino Ratio"] == f"{metrics.sortino_ratio:.2f}"
        assert performance_summary["Buy and Hold Return %"] == f"{metrics.buy_and_hold_return_pct:.2f}%"
        assert performance_summary["Grid Trading Gains"] == f"{metrics.grid_trading_gains:.2f}"
        assert performance_summary["Unrealized PnL"] == f"{metrics.unrealized_pnl:.2f}"
        assert metrics.max_drawdown_duration_hours == 48.0
        assert performance_summary["Total Fees"] == f"{metrics.total_fees:.2f}"
        assert performance_summary["Final Balance (Fiat)"] == f"{metrics.final_balance:.2f}"
        assert performance_summary["Number of Buy Trades"] == metrics.num_buy_trades
        assert performance_summary["Number of Sell Trades"] == metrics.num_sell_trades
        assert metrics.equity_coverage == 1.0
        assert metrics.avg_crypto_exposure is None  # no crypto_value column, as in live
        assert metrics.armed is True
        assert metrics.stop_reason is None

        # Assertions for formatted orders
        assert len(formatted_orders) == 2  # One buy and one sell order
        assert formatted_orders[0] == [
            "BUY",
            "MARKET",
            "CLOSED",
            buy_order.price,
            buy_order.filled,
            "2024-01-01T00:00:00Z",
            grid_level.price,
            "0.00%",  # Slippage for the buy order
        ]
        assert formatted_orders[1] == [
            "SELL",
            "MARKET",
            "CLOSED",
            sell_order.price,
            sell_order.filled,
            "2024-01-02T00:00:00Z",
            grid_level.price,
            "20.00%",  # Slippage for the sell order
        ]

        # Validate logging output
        log_messages = [record.message for record in caplog.records]
        assert any("Formatted Orders" in message for message in log_messages)
        assert any("Performance Summary" in message for message in log_messages)

    def test_calculate_sortino_ratio(self, setup_performance_analyzer, mock_account_data):
        analyzer, _, _ = setup_performance_analyzer
        sortino_ratio = analyzer._calculate_sortino_ratio(mock_account_data)
        assert isinstance(sortino_ratio, float)

    def test_calculate_sortino_ratio_no_downside(self, setup_performance_analyzer):
        analyzer, _, _ = setup_performance_analyzer
        data = pd.DataFrame(
            {"account_value": [10000, 10050, 10100]},
            index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
        )
        sortino_ratio = analyzer._calculate_sortino_ratio(data)
        assert sortino_ratio > 0  # Expected positive Sortino ratio with no downside volatility

    def test_calculate_trade_counts(self, setup_performance_analyzer):
        analyzer, _, order_book = setup_performance_analyzer
        order_book.get_all_buy_orders.return_value = [Mock(), Mock()]
        order_book.get_all_sell_orders.return_value = [Mock()]

        num_buy_trades, num_sell_trades = analyzer._calculate_trade_counts()
        assert num_buy_trades == 2
        assert num_sell_trades == 1

    def test_calculate_buy_and_hold_return(self, setup_performance_analyzer, mock_account_data):
        analyzer, _, _ = setup_performance_analyzer
        initial_price = mock_account_data["close"].iloc[0]
        final_price = 200
        buy_and_hold_return = analyzer._calculate_buy_and_hold_return(mock_account_data, initial_price, final_price)
        expected_return = ((final_price - initial_price) / initial_price) * 100
        assert buy_and_hold_return == expected_return
