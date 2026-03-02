from .order import OrderSide, OrderType


class OrderExecutionFailedError(Exception):
    def __init__(
        self,
        message: str,
        order_side: OrderSide | None = None,
        order_type: OrderType | None = None,
        pair: str | None = None,
        quantity: float | None = None,
        price: float | None = None,
    ):
        super().__init__(message)
        self.order_side = order_side
        self.order_type = order_type
        self.pair = pair
        self.quantity = quantity
        self.price = price
