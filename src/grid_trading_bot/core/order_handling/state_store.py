from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ArmingFlags:
    initial_purchase_done: bool = False
    grid_orders_initialized: bool = False


class StateStore(Protocol):
    async def save(self, arming: ArmingFlags) -> None:
        """Saves the whole trading state with these arming flags."""
