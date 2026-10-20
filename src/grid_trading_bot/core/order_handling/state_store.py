from dataclasses import dataclass
from typing import Protocol

from .placement_journal import PendingPlacement


@dataclass(frozen=True)
class ArmingFlags:
    initial_purchase_done: bool = False
    grid_orders_initialized: bool = False


class StateStore(Protocol):
    async def save(self, arming: ArmingFlags) -> None:
        """Saves the whole trading state with these arming flags."""

    async def save_pending(self, entry: PendingPlacement) -> bool:
        """Writes one placement journal entry before its order is sent; False when it could not be written."""
