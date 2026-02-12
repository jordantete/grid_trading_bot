from unittest.mock import Mock, patch

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pytest

from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderType
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.strategies.plotter import Plotter


class TestPlotter:
    @pytest.fixture
    def setup_plotter(self):
        grid_manager = Mock(spec=GridManager)
        order_book = Mock(spec=OrderBook)
        plotter = Plotter(grid_manager=grid_manager, order_book=order_book)
        return plotter, grid_manager, order_book

    def test_add_grid_lines(self, setup_plotter):
        plotter, grid_manager, _ = setup_plotter
        fig = go.Figure()

        mock_x_data = [1, 2, 3]  # Example x-axis values
        fig.add_trace(go.Scatter(x=mock_x_data, y=[100, 105, 110]))  # Add a dummy trace

        grid_manager.price_grids = [90, 100, 110]
        grid_manager.central_price = 100

        plotter._add_grid_lines(fig, grid_manager.price_grids, grid_manager.central_price)

        assert len(fig.data) == 4  # 1 dummy trace + 3 grid lines
        assert fig.data[1].line.color == "green"  # Below central price
        assert fig.data[2].line.color == "red"  # Above central price
        assert fig.data[3].line.color == "red"  # Above central price

    def test_add_trigger_price_line(self, setup_plotter):
        plotter, grid_manager, _ = setup_plotter
        fig = go.Figure()
        trigger_price = 105

        fig.add_trace(go.Scatter(x=[1, 2, 3], y=[100, 105, 110]))

        plotter._add_trigger_price_line(fig, trigger_price)

        assert len(fig.data) == 2  # 1 dummy trace + 1 trigger price line
        assert fig.data[1].line.color == "blue"
        assert "Trigger Price" in fig.layout.annotations[0].text

    def test_add_trade_markers(self, setup_plotter):
        plotter, _, order_book = setup_plotter
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.70, 0.15, 0.15], vertical_spacing=0.02)

        orders = [
            Order(
                identifier="123",
                status=None,
                order_type=OrderType.LIMIT,
                side=OrderSide.BUY,
                price=1000.0,
                average=None,
                amount=5.0,
                filled=5.0,
                remaining=0.0,
                timestamp=1695890800,
                datetime="2024-01-01T00:00:00Z",
                last_trade_timestamp=1695890800,
                symbol="BTC/USDT",
                time_in_force="GTC",
            ),
            Order(
                identifier="124",
                status=None,
                order_type=OrderType.LIMIT,
                side=OrderSide.SELL,
                price=1200.0,
                average=None,
                amount=3.0,
                filled=3.0,
                remaining=0.0,
                timestamp=1695890800,
                datetime="2024-01-02T00:00:00Z",
                last_trade_timestamp=1695890800,
                symbol="BTC/USDT",
                time_in_force="GTC",
            ),
        ]
        order_book.get_completed_orders.return_value = orders

        plotter._add_trade_markers(fig, orders)

        assert len(fig.data) == 2
        assert fig.data[0].marker.color == "green"
        assert fig.data[1].marker.color == "red"

    def test_add_volume_trace(self, setup_plotter):
        plotter, _, _ = setup_plotter
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.70, 0.15, 0.15], vertical_spacing=0.02)

        data = pd.DataFrame(
            {"open": [100, 110], "close": [110, 105], "volume": [500, 700]},
            index=pd.date_range("2024-01-01", periods=2),
        )

        plotter._add_volume_trace(fig, data)

        assert len(fig.data) == 1
        assert fig.data[0].type == "bar"
        assert fig.data[0].y.tolist() == [500, 700]
        assert list(fig.data[0].marker.color) == ["green", "red"]

    def test_add_account_value_trace(self, setup_plotter):
        plotter, _, _ = setup_plotter
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.70, 0.15, 0.15], vertical_spacing=0.02)
        data = pd.DataFrame({"account_value": [10000, 10500]}, index=pd.date_range("2024-01-01", periods=2))

        plotter._add_account_value_trace(fig, data)

        assert len(fig.data) == 1
        assert fig.data[0].type == "scatter"
        assert fig.data[0].y.tolist() == [10000, 10500]
        assert fig.data[0].line.color == "purple"

    @patch("plotly.graph_objects.Figure.show")
    def test_plot_results(self, mock_show, setup_plotter):
        plotter, grid_manager, order_book = setup_plotter
        data = pd.DataFrame(
            {
                "open": [100, 105],
                "high": [110, 115],
                "low": [95, 100],
                "close": [105, 110],
                "volume": [500, 700],
                "account_value": [10000, 10500],
            },
            index=pd.date_range("2024-01-01", periods=2),
        )

        grid_manager.price_grids = [90, 100, 110]
        grid_manager.central_price = 100
        grid_manager.get_trigger_price.return_value = 100
        order_book.get_completed_orders.return_value = []

        plotter.plot_results(data)
        mock_show.assert_called_once()
