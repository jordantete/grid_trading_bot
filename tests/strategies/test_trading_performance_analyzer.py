import logging
from unittest.mock import Mock

import pandas as pd
import pytest

from grid_trading_bot.core.grid_management.grid_level import GridLevel
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus, OrderType
from grid_trading_bot.strategies.trading_performance_analyzer import TradingPerformanceAnalyzer


class TestPerformanceAnalyzer:
    @pytest.fixture
    def setup_performance_analyzer(self):
        config_manager = Mock()
        config_manager.get_initial_balance.return_value = 10000
        config_manager.get_base_currency.return_value = "BTC"
        config_manager.get_quote_currency.return_value = "USDT"
        config_manager.get_trading_fee.return_value = 0.001
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

    def test_calculate_drawdown(self, setup_performance_analyzer, mock_account_data):
        analyzer, _, _ = setup_performance_analyzer
        max_drawdown = analyzer._calculate_drawdown(mock_account_data)
        assert max_drawdown == pytest.approx(9.52, rel=1e-3)

    def test_calculate_runup(self, setup_performance_analyzer, mock_account_data):
        analyzer, _, _ = setup_performance_analyzer
        max_runup = analyzer._calculate_runup(mock_account_data)
        assert max_runup == 5.0  # Expected max runup from 10000 to 10500 (5%)

    def test_calculate_trading_gains(self, setup_performance_analyzer):
        analyzer, _, order_book = setup_performance_analyzer

        buy_order_1 = Mock(spec=Order, amount=1.0, price=1000.0, fee={"cost": 2.0}, is_filled=Mock(return_value=True))
        buy_order_2 = Mock(spec=Order, amount=0.5, price=1100.0, fee={"cost": 1.0}, is_filled=Mock(return_value=True))

        sell_order_1 = Mock(spec=Order, amount=1.0, price=1200.0, fee={"cost": 1.5}, is_filled=Mock(return_value=True))
        sell_order_2 = Mock(spec=Order, amount=0.5, price=1300.0, fee={"cost": 0.5}, is_filled=Mock(return_value=True))

        order_book.get_all_buy_orders.return_value = [buy_order_1, buy_order_2]
        order_book.get_all_sell_orders.return_value = [sell_order_1, sell_order_2]

        result = analyzer._calculate_trading_gains()

        # Total buy cost: (1 * 1000 + 2) + (0.5 * 1100 + 1) = 1002 + 551 = 1553
        # Total sell revenue: (1 * 1200 - 1.5) + (0.5 * 1300 - 0.5) = 1198.5 + 649.5 = 1848
        # Gains: 1848 - 1553 = 295.00
        assert result == "295.00"

    def test_calculate_trading_gains_zero_trades(self, setup_performance_analyzer):
        analyzer, _, order_book = setup_performance_analyzer
        order_book.get_all_buy_orders.return_value = []
        order_book.get_all_sell_orders.return_value = []

        trading_gains = analyzer._calculate_trading_gains()
        assert trading_gains == "N/A"

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
            performance_summary, formatted_orders = analyzer.generate_performance_summary(
                mock_account_data,
                initial_price,
                final_fiat_balance,
                final_crypto_balance,
                final_crypto_price,
                total_fees,
            )

        # Assertions for performance summary
        assert (
            performance_summary["Pair"] == f"{config_manager.get_base_currency()}/{config_manager.get_quote_currency()}"
        )
        assert performance_summary["Start Date"] == mock_account_data.index[0]
        assert performance_summary["End Date"] == mock_account_data.index[-1]
        assert performance_summary["Duration"] == mock_account_data.index[-1] - mock_account_data.index[0]
        expected_roi = analyzer._calculate_roi(
            initial_balance,
            final_fiat_balance + final_crypto_balance * final_crypto_price,
        )
        assert performance_summary["ROI"] == f"{expected_roi:.2f}%"
        assert performance_summary["Grid Trading Gains"] == "197.50"  # Adjusted for mocked fees
        assert performance_summary["Total Fees"] == f"{total_fees:.2f}"
        assert (
            performance_summary["Final Balance (Fiat)"]
            == f"{final_fiat_balance + final_crypto_balance * final_crypto_price:.2f}"
        )
        assert (
            performance_summary["Final Crypto Balance"]
            == f"{final_crypto_balance:.4f} {config_manager.get_base_currency()}"
        )
        assert (
            performance_summary["Remaining Fiat Balance"]
            == f"{final_fiat_balance:.2f} {config_manager.get_quote_currency()}"
        )
        assert performance_summary["Number of Buy Trades"] == 1
        assert performance_summary["Number of Sell Trades"] == 1
        assert "Sharpe Ratio" in performance_summary
        assert "Sortino Ratio" in performance_summary

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
