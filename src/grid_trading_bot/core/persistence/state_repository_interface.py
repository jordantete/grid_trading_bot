from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StateSnapshot:
    bot_state: dict[str, Any]
    balance_state: dict[str, str | None]
    orders: list[dict[str, Any]]
    grid_levels: list[dict[str, Any]]


class StateRepositoryInterface(ABC):
    @abstractmethod
    def initialize(self) -> None:
        pass

    @abstractmethod
    def save_state(self, snapshot: StateSnapshot) -> None:
        """Replaces the whole saved state with `snapshot` in one transaction."""

    @abstractmethod
    def load_bot_state(self) -> dict[str, Any] | None:
        pass

    @abstractmethod
    def load_balance_state(self) -> dict[str, str | None] | None:
        pass

    @abstractmethod
    def load_all_orders(self) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def load_grid_levels(self) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def close(self) -> None:
        pass
