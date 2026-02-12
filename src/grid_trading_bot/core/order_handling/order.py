from enum import Enum

import pandas as pd


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class Order:
    def __init__(
        self,
        identifier: str,
        status: OrderStatus,
        order_type: OrderType,
        side: OrderSide,
        price: float,
        average: float | None,
        amount: float,
        filled: float,
        remaining: float,
        timestamp: int,
        datetime: str | None,
        last_trade_timestamp: int | None,
        symbol: str,
        time_in_force: str | None,
        trades: list[dict[str, str | float]] | None = None,
        fee: dict[str, str | float] | None = None,
        cost: float | None = None,
        info: dict[str, str | float | dict] | None = None,
    ):
        self.identifier = identifier
        self.status = status  # 'open', 'closed', 'canceled', 'expired', 'rejected'
        self.order_type = order_type  # 'market', 'limit'
        self.side = side  # 'buy', 'sell'
        self.price = price  # float price in quote currency (may be empty for market orders)
        self.average = average  # float average filling price
        self.amount = amount  # ordered amount of base currency
        self.filled = filled  # filled amount of base currency
        self.remaining = remaining  # remaining amount to fill
        self.timestamp = timestamp  # order placing/opening Unix timestamp in milliseconds
        self.datetime = datetime  # ISO8601 datetime of 'timestamp' with milliseconds
        self.last_trade_timestamp = last_trade_timestamp  # Unix timestamp of the most recent trade on this order
        self.symbol = symbol  # symbol
        self.time_in_force = time_in_force  # 'GTC', 'IOC', 'FOK', 'PO'
        self.trades = trades  # a list of order trades/executions
        self.fee = fee  # fee info, if available
        self.cost = cost  # 'filled' * 'price' (filling price used where available)
        self.info = info  # Original unparsed structure for debugging or auditing

    def is_filled(self) -> bool:
        return self.status == OrderStatus.CLOSED

    def is_canceled(self) -> bool:
        return self.status == OrderStatus.CANCELED

    def is_open(self) -> bool:
        return self.status == OrderStatus.OPEN

    def format_last_trade_timestamp(self) -> str | None:
        if self.last_trade_timestamp is None:
            return None
        return pd.Timestamp(self.last_trade_timestamp, unit="s").isoformat()

    def __str__(self) -> str:
        return (
            f"Order(id={self.identifier}, status={self.status}, "
            f"type={self.order_type}, side={self.side}, price={self.price}, average={self.average}, "
            f"amount={self.amount}, filled={self.filled}, remaining={self.remaining}, "
            f"timestamp={self.timestamp}, datetime={self.datetime}, symbol={self.symbol}, "
            f"time_in_force={self.time_in_force}, trades={self.trades}, fee={self.fee}, cost={self.cost})"
        )

    def __repr__(self) -> str:
        return self.__str__()
