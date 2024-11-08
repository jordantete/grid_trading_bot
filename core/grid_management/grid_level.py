from enum import Enum, auto
from typing import List, Optional
from ..order_handling.order import Order

class GridCycleState(Enum):
    READY_TO_BUY = auto()
    READY_TO_SELL = auto()
    COMPLETED = auto()   

class GridLevel:
    def __init__(self, price: float, cycle_state: GridCycleState):
        self.price: float = price
        self.buy_orders: List[Order] = []
        self.sell_orders: List[Order] = []
        self.cycle_state: GridCycleState = cycle_state
    
    def place_buy_order(self, buy_order: Order) -> None:
        self.buy_orders.append(buy_order)
        self.cycle_state = GridCycleState.READY_TO_SELL

    def place_sell_order(self, sell_order: Order) -> None:
        self.sell_orders.append(sell_order)
    
    def can_place_buy_order(self) -> bool:
        return self.cycle_state == GridCycleState.READY_TO_BUY

    def can_place_sell_order(self) -> bool:
        return self.cycle_state == GridCycleState.READY_TO_SELL
    
    def reset_buy_level_cycle(self) -> None:
        self.cycle_state = GridCycleState.READY_TO_BUY
    
    def __str__(self) -> str:
        latest_buy_order: Optional[Order] = self.buy_orders[-1] if self.buy_orders else None
        latest_sell_order: Optional[Order] = self.sell_orders[-1] if self.sell_orders else None
        return (
            f"GridLevel(price={self.price}, "
            f"cycle_state={self.cycle_state.name}, "
            f"num_buy_orders={len(self.buy_orders)}, "
            f"num_sell_orders={len(self.sell_orders)}, "
            f"latest_buy_order={latest_buy_order}, "
            f"latest_sell_order={latest_sell_order})"
        )

    def __repr__(self) -> str:
        return self.__str__()