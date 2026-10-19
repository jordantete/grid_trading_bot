import copy
from datetime import UTC, datetime
import logging

import pandas as pd

from grid_trading_bot.core.grid_management.grid_manager import GridManager

from .order import Order, OrderSide, OrderStatus
from .order_book import OrderBook
from .order_update_handler import OrderUpdateHandler


class OrderSimulator:
    """Simulates order fills during backtesting by checking if grid levels were crossed."""

    def __init__(
        self,
        order_book: OrderBook,
        grid_manager: GridManager,
        order_update_handler: OrderUpdateHandler,
        slippage: float = 0.0,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.order_book = order_book
        self.grid_manager = grid_manager
        self.order_update_handler = order_update_handler
        self.slippage = slippage

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

        crossed_buys = [o for o in pending_orders if o.side == OrderSide.BUY and o.price in crossed_buy_levels]
        crossed_sells = [o for o in pending_orders if o.side == OrderSide.SELL and o.price in crossed_sell_levels]

        # Fills follow the order the price reaches the levels, so each fill finds the level it frees already processed.
        crossed_buys.sort(key=lambda o: o.price, reverse=True)
        crossed_sells.sort(key=lambda o: o.price)

        for order in [*crossed_buys, *crossed_sells]:
            await self.simulate_fill(order, timestamp_val)

    async def simulate_fill(self, order: Order, timestamp: int) -> None:
        """Hands the fill of a crossed order to the order manager, which applies it to the order."""
        fill = copy.copy(order)
        fill.filled = order.amount
        fill.remaining = 0.0
        fill.status = OrderStatus.CLOSED
        if self.slippage:
            fill.average = (
                order.price * (1 + self.slippage) if order.side == OrderSide.BUY else order.price * (1 - self.slippage)
            )
        fill.timestamp = timestamp
        fill.last_trade_timestamp = timestamp
        timestamp_in_seconds = timestamp / 1000 if timestamp > 10**10 else timestamp
        formatted_timestamp = datetime.fromtimestamp(timestamp_in_seconds, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
        self.logger.info(
            f"Simulated fill for {order.side.value.upper()} order at price {order.price} "
            f"with amount {order.amount}. Filled at timestamp {formatted_timestamp}",
        )
        await self.order_update_handler.apply_order_update(fill)
