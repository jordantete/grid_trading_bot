"""In-memory order execution: placed orders rest until a test fills or ends them."""

import asyncio
import copy
from decimal import ROUND_DOWN, Decimal
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
        self.client_ids: dict[str, str] = {}
        self.calls: list[str] = []
        self.fail_next_limit_order: Exception | None = None
        self.lose_next_limit_response: Exception | None = None
        self.market_order_failures: list[Exception] = []
        self.partial_market_fill: float | None = None
        self.amount_decimals: int | None = None
        self.price_decimals: int | None = None
        self.fill_limit_orders_on_placement = False
        self.placement_delay = 0.0
        self._ids = itertools.count(1)

    async def execute_market_order(self, order_side, pair, quantity, price, client_order_id):
        self.calls.append("execute_market_order")
        if self.market_order_failures:
            raise self.market_order_failures.pop(0)
        filled = quantity
        if self.partial_market_fill is not None:
            filled, self.partial_market_fill = quantity * self.partial_market_fill, None
        order = self._create(order_side, OrderType.MARKET, pair, quantity, self.market_price, filled, client_order_id)
        # A market order never rests: what did not fill is cancelled, as the live strategy reports it.
        order.status = OrderStatus.CLOSED
        return copy.copy(order)

    async def execute_limit_order(self, order_side, pair, quantity, price, client_order_id):
        self.calls.append("execute_limit_order")
        if self.placement_delay:
            await asyncio.sleep(self.placement_delay)
        if self.fail_next_limit_order is not None:
            error, self.fail_next_limit_order = self.fail_next_limit_order, None
            raise error
        filled = quantity if self.fill_limit_orders_on_placement else 0.0
        order = self._create(order_side, OrderType.LIMIT, pair, quantity, price, filled, client_order_id)
        if self.lose_next_limit_response is not None:
            error, self.lose_next_limit_response = self.lose_next_limit_response, None
            raise error
        return copy.copy(order)

    async def get_order(self, order_id, pair):
        self.calls.append("get_order")
        if order_id not in self.orders:
            raise OrderNotFoundError(f"Order {order_id} not found on {pair}")
        return copy.copy(self.orders[order_id])

    async def find_order(self, client_order_id, pair):
        self.calls.append("find_order")
        order_id = self.client_ids.get(client_order_id)
        return copy.copy(self.orders[order_id]) if order_id is not None else None

    async def cancel_order(self, order_id, pair):
        self.calls.append("cancel_order")
        order = self.orders[order_id]
        if order.status != OrderStatus.OPEN:
            return False
        order.status = OrderStatus.CANCELED
        return True

    def round_amount(self, pair, amount):
        if self.amount_decimals is None:
            return amount
        step = Decimal(1).scaleb(-self.amount_decimals)
        return float(Decimal(str(amount)).quantize(step, rounding=ROUND_DOWN))

    def round_price(self, pair, price):
        return price if self.price_decimals is None else round(price, self.price_decimals)

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

    def _create(self, side, order_type, pair, quantity, price, filled, client_order_id=None) -> Order:
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
        if client_order_id is not None:
            self.client_ids[client_order_id] = order.identifier
        return order
