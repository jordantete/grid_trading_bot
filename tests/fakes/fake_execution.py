"""In-memory order execution: placed orders rest until a test fills or ends them."""

import asyncio
import copy
import itertools

from grid_trading_bot.core.order_handling.execution_strategy.order_execution_strategy_interface import (
    OrderExecutionStrategyInterface,
)
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus, OrderType
from grid_trading_bot.core.services.exceptions import OrderNotFoundError


class FakeExecution(OrderExecutionStrategyInterface):
    def __init__(self, market_price: float = 100.0):
        self.market_price = market_price
        self.orders: dict[str, Order] = {}
        self.fail_next_limit_order: Exception | None = None
        self.fill_limit_orders_on_placement = False
        self.placement_delay = 0.0
        self._ids = itertools.count(1)

    async def execute_market_order(self, order_side, pair, quantity, price):
        return copy.copy(self._create(order_side, OrderType.MARKET, pair, quantity, self.market_price, quantity))

    async def execute_limit_order(self, order_side, pair, quantity, price):
        if self.placement_delay:
            await asyncio.sleep(self.placement_delay)
        if self.fail_next_limit_order is not None:
            error, self.fail_next_limit_order = self.fail_next_limit_order, None
            raise error
        filled = quantity if self.fill_limit_orders_on_placement else 0.0
        return copy.copy(self._create(order_side, OrderType.LIMIT, pair, quantity, price, filled))

    async def get_order(self, order_id, pair):
        if order_id not in self.orders:
            raise OrderNotFoundError(f"Order {order_id} not found on {pair}")
        return copy.copy(self.orders[order_id])

    async def cancel_order(self, order_id, pair):
        order = self.orders[order_id]
        if order.status != OrderStatus.OPEN:
            return False
        order.status = OrderStatus.CANCELED
        return True

    def open_orders(self, side: OrderSide | None = None) -> list[Order]:
        return [o for o in self.orders.values() if o.status == OrderStatus.OPEN and side in (None, o.side)]

    def order_at(self, price: float, side: OrderSide) -> Order:
        """The single open order resting at `price` on `side`."""
        matches = [o for o in self.open_orders(side) if o.price == price]
        assert len(matches) == 1, f"expected one open {side.value} at {price}, found {matches}"
        return matches[0]

    def fill(self, order_id: str, quantity: float | None = None) -> Order:
        """Fills `quantity` more of an open order (the rest of it by default) and returns the exchange's view."""
        order = self.orders[order_id]
        if quantity is None or quantity >= order.remaining:
            order.filled, order.remaining, order.status = order.amount, 0.0, OrderStatus.CLOSED
        else:
            order.filled += quantity
            order.remaining = order.amount - order.filled
        order.average = order.price
        return copy.copy(order)

    def end(self, order_id: str, status: OrderStatus) -> Order:
        """Ends an open order as CANCELED, EXPIRED or REJECTED and returns the exchange's view."""
        self.orders[order_id].status = status
        return copy.copy(self.orders[order_id])

    def _create(self, side, order_type, pair, quantity, price, filled) -> Order:
        order = Order(
            identifier=f"o{next(self._ids)}",
            status=OrderStatus.CLOSED if filled >= quantity else OrderStatus.OPEN,
            order_type=order_type,
            side=side,
            price=price,
            average=price if filled else None,
            amount=quantity,
            filled=filled,
            remaining=quantity - filled,
            timestamp=0,
            datetime=None,
            last_trade_timestamp=None,
            symbol=pair,
            time_in_force="GTC",
        )
        self.orders[order.identifier] = order
        return order
