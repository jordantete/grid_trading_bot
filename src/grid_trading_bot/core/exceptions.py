from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grid_trading_bot.core.order_handling.order import Order


class GridBotError(Exception):
    """Root of every exception the bot raises on purpose."""


class BookingError(GridBotError):
    """A market order the exchange executed and the bot could not book: it must never be sent again."""

    def __init__(self, order: "Order", cause: Exception):
        self.order = order
        self.cause = cause
        price = order.average if order.average is not None else order.price
        fee = order.fee if order.fee is not None else "not reported"
        super().__init__(
            f"{order.side.value} market order {order.identifier} executed ({order.filled} at {price}, fee {fee}) "
            f"but could not be booked: {cause}"
        )


class OrderRefusedError(GridBotError):
    """An order refused for a business reason, by the bot's own checks or by the exchange: an expected outcome."""
