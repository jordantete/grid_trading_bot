import pandas as pd
import pytest

from grid_trading_bot.core.order_handling.backtest_clock import BacktestClock
from grid_trading_bot.core.order_handling.execution_strategy.backtest_order_execution_strategy import (
    BacktestOrderExecutionStrategy,
)
from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus, OrderType


@pytest.mark.asyncio
class TestBacktestOrderExecutionStrategy:
    @pytest.fixture
    def setup_strategy(self):
        clock = BacktestClock()
        clock.set(pd.Timestamp("2024-08-01 00:01:00"))
        return BacktestOrderExecutionStrategy(clock=clock)

    async def test_execute_market_order(self, setup_strategy):
        strategy = setup_strategy
        order_side = OrderSide.BUY
        pair = "BTC/USDT"
        quantity = 0.5
        price = 30000

        order = await strategy.execute_market_order(order_side, pair, quantity, price, "g1")

        assert order is not None
        assert order.identifier == "backtest-0"
        assert order.status == OrderStatus.CLOSED
        assert order.order_type == OrderType.MARKET
        assert order.side == order_side
        assert order.price == price
        assert order.amount == quantity
        assert order.filled == quantity
        assert order.remaining == 0
        assert order.symbol == pair
        assert order.time_in_force == "GTC"
        assert order.timestamp == 1722470460000
        assert order.last_trade_timestamp == 1722470460000

    async def test_execute_limit_order(self, setup_strategy):
        strategy = setup_strategy
        order_side = OrderSide.SELL
        pair = "ETH/USDT"
        quantity = 1
        price = 2000

        order = await strategy.execute_limit_order(order_side, pair, quantity, price, "g1")

        assert order is not None
        assert order.identifier == "backtest-0"
        assert order.status == OrderStatus.OPEN
        assert order.order_type == OrderType.LIMIT
        assert order.side == order_side
        assert order.price == price
        assert order.amount == quantity
        assert order.filled == 0
        assert order.remaining == quantity
        assert order.symbol == pair
        assert order.time_in_force == "GTC"
        assert order.timestamp == 1722470460000
        assert order.last_trade_timestamp is None

    async def test_execute_market_buy_order_with_slippage(self):
        strategy = BacktestOrderExecutionStrategy(slippage=0.001)
        order = await strategy.execute_market_order(OrderSide.BUY, "BTC/USDT", 0.5, 30000, "g1")

        assert order is not None
        assert order.price == 30000
        assert order.average == 30000 * 1.001
        assert order.side == OrderSide.BUY

    async def test_execute_market_sell_order_with_slippage(self):
        strategy = BacktestOrderExecutionStrategy(slippage=0.001)
        order = await strategy.execute_market_order(OrderSide.SELL, "BTC/USDT", 0.5, 30000, "g1")

        assert order is not None
        assert order.price == 30000
        assert order.average == 30000 * 0.999
        assert order.side == OrderSide.SELL

    async def test_execute_market_order_no_slippage_by_default(self):
        strategy = BacktestOrderExecutionStrategy()
        order = await strategy.execute_market_order(OrderSide.BUY, "BTC/USDT", 0.5, 30000, "g1")

        assert order.price == 30000
        assert order.average == 30000

    async def test_order_ids_are_unique_within_the_same_second(self, setup_strategy):
        """Orders placed on the same candle must not collide on identifier."""
        strategy = setup_strategy

        market_order = await strategy.execute_market_order(OrderSide.BUY, "BTC/USDT", 0.5, 30000, "g1")
        limit_order = await strategy.execute_limit_order(OrderSide.SELL, "BTC/USDT", 0.5, 30000, "g2")
        another_market_order = await strategy.execute_market_order(OrderSide.BUY, "BTC/USDT", 0.5, 30000, "g3")

        ids = {market_order.identifier, limit_order.identifier, another_market_order.identifier}
        assert len(ids) == 3

    async def test_without_a_clock_the_orders_carry_time_zero(self):
        strategy = BacktestOrderExecutionStrategy()

        order = await strategy.execute_market_order(OrderSide.BUY, "SOL/USDT", 1.0, 100.0, "g1")

        assert (order.timestamp, order.last_trade_timestamp) == (0, 0)

    async def test_get_order_returns_none(self, setup_strategy):
        assert await setup_strategy.get_order("any-id", "BTC/USDT") is None
