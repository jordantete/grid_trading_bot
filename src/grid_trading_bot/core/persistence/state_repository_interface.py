from abc import ABC, abstractmethod
from typing import Any


class StateRepositoryInterface(ABC):
    @abstractmethod
    def initialize(self) -> None:
        pass

    @abstractmethod
    def save_bot_state(self, state: dict[str, Any]) -> None:
        pass

    @abstractmethod
    def load_bot_state(self) -> dict[str, Any] | None:
        pass

    @abstractmethod
    def save_balance_state(self, state: dict[str, str]) -> None:
        pass

    @abstractmethod
    def load_balance_state(self) -> dict[str, str] | None:
        pass

    @abstractmethod
    def save_order(self, order_dict: dict[str, Any]) -> None:
        pass

    @abstractmethod
    def save_orders(self, order_dicts: list[dict[str, Any]]) -> None:
        pass

    @abstractmethod
    def load_all_orders(self) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def save_grid_level(self, grid_level_dict: dict[str, Any]) -> None:
        pass

    @abstractmethod
    def save_grid_levels(self, grid_level_dicts: list[dict[str, Any]]) -> None:
        pass

    @abstractmethod
    def load_grid_levels(self) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def clear_all(self) -> None:
        pass

    @abstractmethod
    def close(self) -> None:
        pass
