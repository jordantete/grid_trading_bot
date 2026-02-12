from enum import Enum

from ..order_handling.order import Order


class GridCycleState(Enum):
    READY_TO_BUY_OR_SELL = "ready_to_buy_or_sell"  # Level is ready for both a buy or a sell order
    READY_TO_BUY = "ready_to_buy"  # Level is ready for a buy order
    WAITING_FOR_BUY_FILL = "waiting_for_buy_fill"  # Buy order placed, waiting for execution
    READY_TO_SELL = "ready_to_sell"  # Level is ready for a sell order
    WAITING_FOR_SELL_FILL = "waiting_for_sell_fill"  # Sell order placed, waiting for execution


class GridLevel:
    def __init__(self, price: float, state: GridCycleState):
        self.price: float = price
        self.orders: list[Order] = []  # Track all orders at this level
        self.state: GridCycleState = state
        self.paired_buy_level: GridLevel | None = None
        self.paired_sell_level: GridLevel | None = None

    def add_order(self, order: Order) -> None:
        """
        Record an order at this level.
        """
        self.orders.append(order)

    def __str__(self) -> str:
        return (
            f"GridLevel(price={self.price}, "
            f"state={self.state.name}, "
            f"num_orders={len(self.orders)}, "
            f"paired_buy_level={self.paired_buy_level.price if self.paired_buy_level else None}), "
            f"paired_sell_level={self.paired_sell_level.price if self.paired_sell_level else None})"
        )

    def __repr__(self) -> str:
        return self.__str__()
