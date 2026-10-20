"""
An in-memory state store: records every save and every journal entry written, and can be told to fail. Given an
`observe` callable, it also records what that callable returns at each save, so a test can check the state a save
photographed.
"""

from collections.abc import Callable
import copy
from typing import Any

from grid_trading_bot.core.order_handling.placement_journal import PendingPlacement
from grid_trading_bot.core.order_handling.state_store import ArmingFlags


class RecordingStore:
    def __init__(self, observe: Callable[[], Any] | None = None):
        self.observe = observe
        self.saves: list[ArmingFlags] = []
        self.observed: list[Any] = []
        self.pending: list[PendingPlacement] = []
        self.fail_pending = False

    async def save(self, arming: ArmingFlags) -> None:
        self.saves.append(arming)
        if self.observe is not None:
            self.observed.append(self.observe())

    async def save_pending(self, entry: PendingPlacement) -> bool:
        if self.fail_pending:
            return False
        self.pending.append(copy.copy(entry))
        return True
