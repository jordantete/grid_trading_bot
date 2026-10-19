from typing import Protocol

from .order import Order


class OrderUpdateHandler(Protocol):
    async def apply_order_update(self, update: Order) -> None:
        """Applies the exchange's view of one order."""
