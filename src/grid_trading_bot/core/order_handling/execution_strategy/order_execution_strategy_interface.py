from abc import ABC, abstractmethod

from ..order import Order, OrderSide


class OrderExecutionStrategyInterface(ABC):
    @abstractmethod
    async def execute_market_order(
        self,
        order_side: OrderSide,
        pair: str,
        quantity: float,
        price: float,
        client_order_id: str,
    ) -> Order:
        """Sends one market order under `client_order_id` and returns it closed with what it executed."""

    @abstractmethod
    async def execute_limit_order(
        self,
        order_side: OrderSide,
        pair: str,
        quantity: float,
        price: float,
        client_order_id: str,
    ) -> Order:
        """Places one limit order under `client_order_id`."""

    @abstractmethod
    async def get_order(
        self,
        order_id: str,
        pair: str,
    ) -> Order | None:
        pass

    @abstractmethod
    async def find_order(self, client_order_id: str, pair: str) -> Order | None:
        """The order placed under `client_order_id`, or None when the exchange has none."""

    @abstractmethod
    async def cancel_order(self, order_id: str, pair: str) -> bool:
        """Cancels an open order. Returns True if the cancellation succeeded."""

    @abstractmethod
    def round_amount(self, pair: str, amount: float) -> float:
        """`amount` at the exchange's precision for `pair`."""

    @abstractmethod
    def round_price(self, pair: str, price: float) -> float:
        """`price` at the exchange's precision for `pair`."""
