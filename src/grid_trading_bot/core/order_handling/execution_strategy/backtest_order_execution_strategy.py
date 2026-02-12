import time

from ..order import Order, OrderSide, OrderStatus, OrderType
from .order_execution_strategy_interface import OrderExecutionStrategyInterface


class BacktestOrderExecutionStrategy(OrderExecutionStrategyInterface):
    async def execute_market_order(
        self,
        order_side: OrderSide,
        pair: str,
        quantity: float,
        price: float,
    ) -> Order | None:
        order_id = f"backtest-{int(time.time())}"
        timestamp = int(time.time() * 1000)
        return Order(
            identifier=order_id,
            status=OrderStatus.OPEN,
            order_type=OrderType.MARKET,
            side=order_side,
            price=price,
            average=price,
            amount=quantity,
            filled=quantity,
            remaining=0,
            timestamp=timestamp,
            datetime="111",
            last_trade_timestamp=1,
            symbol=pair,
            time_in_force="GTC",
        )

    async def execute_limit_order(
        self,
        order_side: OrderSide,
        pair: str,
        quantity: float,
        price: float,
    ) -> Order | None:
        order_id = f"backtest-{int(time.time())}"
        return Order(
            identifier=order_id,
            status=OrderStatus.OPEN,
            order_type=OrderType.LIMIT,
            side=order_side,
            price=price,
            average=price,
            amount=quantity,
            filled=0,
            remaining=quantity,
            timestamp=0,
            datetime="",
            last_trade_timestamp=1,
            symbol=pair,
            time_in_force="GTC",
        )

    async def get_order(
        self,
        order_id: str,
        pair: str,
    ) -> Order | None:
        return Order(
            identifier=order_id,
            status=OrderStatus.OPEN,
            order_type=OrderType.LIMIT,
            side=OrderSide.BUY,
            price=100,
            average=100,
            amount=1,
            filled=1,
            remaining=0,
            timestamp=0,
            datetime="111",
            last_trade_timestamp=1,
            symbol=pair,
            time_in_force="GTC",
        )
