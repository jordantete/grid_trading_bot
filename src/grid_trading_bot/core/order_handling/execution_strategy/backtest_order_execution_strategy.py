import itertools

import pandas as pd

from ..backtest_clock import BacktestClock
from ..order import Order, OrderSide, OrderStatus, OrderType
from .order_execution_strategy_interface import OrderExecutionStrategyInterface


def _iso(timestamp_ms: int) -> str:
    return pd.Timestamp(timestamp_ms, unit="ms").isoformat()


class BacktestOrderExecutionStrategy(OrderExecutionStrategyInterface):
    def __init__(self, slippage: float = 0.0, clock: BacktestClock | None = None) -> None:
        self.slippage = slippage
        self.clock = clock if clock is not None else BacktestClock()
        self._id_counter = itertools.count()

    async def execute_market_order(
        self,
        order_side: OrderSide,
        pair: str,
        quantity: float,
        price: float,
        client_order_id: str,
    ) -> Order:
        order_id = f"backtest-{next(self._id_counter)}"
        now_ms = self.clock.now_ms
        if self.slippage:
            average = price * (1 + self.slippage) if order_side == OrderSide.BUY else price * (1 - self.slippage)
        else:
            average = price
        return Order(
            identifier=order_id,
            status=OrderStatus.CLOSED,
            order_type=OrderType.MARKET,
            side=order_side,
            price=price,
            average=average,
            amount=quantity,
            filled=quantity,
            remaining=0,
            timestamp=now_ms,
            datetime=_iso(now_ms),
            last_trade_timestamp=now_ms,
            symbol=pair,
            time_in_force="GTC",
        )

    async def execute_limit_order(
        self,
        order_side: OrderSide,
        pair: str,
        quantity: float,
        price: float,
        client_order_id: str,
    ) -> Order:
        order_id = f"backtest-{next(self._id_counter)}"
        now_ms = self.clock.now_ms
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
            timestamp=now_ms,
            datetime=_iso(now_ms),
            last_trade_timestamp=None,
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

    async def find_order(self, client_order_id: str, pair: str) -> Order | None:
        """A backtest order never loses its answer, so there is nothing to find."""
        return None

    def round_amount(self, pair: str, amount: float) -> float:
        return amount

    def round_price(self, pair: str, price: float) -> float:
        return price

    async def cancel_order(self, order_id: str, pair: str) -> bool:
        return True
