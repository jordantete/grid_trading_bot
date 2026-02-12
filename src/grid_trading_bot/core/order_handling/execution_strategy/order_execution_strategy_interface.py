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
    ) -> Order | None:
        pass

    @abstractmethod
    async def execute_limit_order(
        self,
        order_side: OrderSide,
        pair: str,
        quantity: float,
        price: float,
    ) -> Order | None:
        pass

    @abstractmethod
    async def get_order(
        self,
        order_id: str,
        pair: str,
    ) -> Order | None:
        pass
