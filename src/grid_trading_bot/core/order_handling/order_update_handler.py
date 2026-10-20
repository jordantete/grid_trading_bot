from typing import Protocol

from .order import Order


class OrderUpdateHandler(Protocol):
    async def apply_order_update(self, update: Order) -> None:
        """Applies the exchange's view of one order that filled or ended."""

    async def retry_pending_placements(self) -> None:
        """Resolves or sends again every placement still waiting in the journal."""
