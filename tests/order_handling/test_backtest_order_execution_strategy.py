from unittest.mock import patch

import pytest

from grid_trading_bot.core.order_handling.execution_strategy.backtest_order_execution_strategy import (
    BacktestOrderExecutionStrategy,
)
from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus, OrderType


@pytest.mark.asyncio
class TestBacktestOrderExecutionStrategy:
    @pytest.fixture
    def setup_strategy(self):
        return BacktestOrderExecutionStrategy()

    @patch("time.time", return_value=1680000000)
    async def test_execute_market_order(self, mock_time, setup_strategy):
        strategy = setup_strategy
        order_side = OrderSide.BUY
        pair = "BTC/USDT"
        quantity = 0.5
        price = 30000

        order = await strategy.execute_market_order(order_side, pair, quantity, price)

        assert order is not None
        assert order.identifier == "backtest-1680000000"
        assert order.status == OrderStatus.OPEN
        assert order.order_type == OrderType.MARKET
        assert order.side == order_side
        assert order.price == price
        assert order.amount == quantity
        assert order.filled == quantity
        assert order.remaining == 0
        assert order.symbol == pair
        assert order.time_in_force == "GTC"
        assert order.timestamp == 1680000000000

    @patch("time.time", return_value=1680000000)
    async def test_execute_limit_order(self, mock_time, setup_strategy):
        strategy = setup_strategy
        order_side = OrderSide.SELL
        pair = "ETH/USDT"
        quantity = 1
        price = 2000

        order = await strategy.execute_limit_order(order_side, pair, quantity, price)

        assert order is not None
        assert order.identifier == "backtest-1680000000"
        assert order.status == OrderStatus.OPEN
        assert order.order_type == OrderType.LIMIT
        assert order.side == order_side
        assert order.price == price
        assert order.amount == quantity
        assert order.filled == 0
        assert order.remaining == quantity
        assert order.symbol == pair
        assert order.time_in_force == "GTC"
        assert order.timestamp == 0

    async def test_get_order(self, setup_strategy):
        strategy = setup_strategy
        order_id = "test-order-1"
        order_symbol = "BTC/USDT"

        order = await strategy.get_order(order_id, order_symbol)

        assert order is not None
        assert order.identifier == order_id
        assert order.status == OrderStatus.OPEN
        assert order.order_type == OrderType.LIMIT
        assert order.side == OrderSide.BUY
        assert order.price == 100
        assert order.average == 100
        assert order.amount == 1
        assert order.filled == 1
        assert order.remaining == 0
        assert order.symbol == order_symbol
        assert order.time_in_force == "GTC"
        assert order.timestamp == 0
