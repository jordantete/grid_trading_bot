from datetime import UTC, datetime
import logging

import pandas as pd

from core.bot_management.event_bus import EventBus, Events
from core.grid_management.grid_manager import GridManager

from .order import Order, OrderSide, OrderStatus
from .order_book import OrderBook


class OrderSimulator:
    """Simulates order fills during backtesting by checking if grid levels were crossed."""

    def __init__(
        self,
        order_book: OrderBook,
        grid_manager: GridManager,
        event_bus: EventBus,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.order_book = order_book
        self.grid_manager = grid_manager
        self.event_bus = event_bus

    async def simulate_order_fills(
        self,
        high_price: float,
        low_price: float,
        timestamp: int | pd.Timestamp,
    ) -> None:
        """
        Simulates the execution of limit orders based on crossed grid levels within the high-low price range.

        Args:
            high_price: The highest price reached in this time interval.
            low_price: The lowest price reached in this time interval.
            timestamp: The current timestamp in the backtest simulation.
        """
        timestamp_val = int(timestamp.timestamp()) if isinstance(timestamp, pd.Timestamp) else int(timestamp)
        pending_orders = self.order_book.get_open_orders()
        crossed_buy_levels = {level for level in self.grid_manager.sorted_buy_grids if low_price <= level <= high_price}
        crossed_sell_levels = {
            level for level in self.grid_manager.sorted_sell_grids if low_price <= level <= high_price
        }

        self.logger.debug(
            f"Simulating fills: High {high_price}, Low {low_price}, Pending orders: {len(pending_orders)}",
        )
        self.logger.debug(f"Crossed buy levels: {crossed_buy_levels}, Crossed sell levels: {crossed_sell_levels}")

        for order in pending_orders:
            if (order.side == OrderSide.BUY and order.price in crossed_buy_levels) or (
                order.side == OrderSide.SELL and order.price in crossed_sell_levels
            ):
                await self._simulate_fill(order, timestamp_val)

    async def _simulate_fill(
        self,
        order: Order,
        timestamp: int,
    ) -> None:
        """
        Simulates filling an order by marking it as completed and publishing an event.

        Args:
            order: The order to simulate a fill for.
            timestamp: The timestamp at which the order is filled.
        """
        order.filled = order.amount
        order.remaining = 0.0
        order.status = OrderStatus.CLOSED
        self.order_book.remove_open_order(order)
        order.timestamp = timestamp
        order.last_trade_timestamp = timestamp
        timestamp_in_seconds = timestamp / 1000 if timestamp > 10**10 else timestamp
        formatted_timestamp = datetime.fromtimestamp(timestamp_in_seconds, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
        self.logger.info(
            f"Simulated fill for {order.side.value.upper()} order at price {order.price} "
            f"with amount {order.amount}. Filled at timestamp {formatted_timestamp}",
        )
        await self.event_bus.publish(Events.ORDER_FILLED, order)
