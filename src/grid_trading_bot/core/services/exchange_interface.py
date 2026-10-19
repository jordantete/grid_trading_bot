from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class ExchangeInterface(ABC):
    @abstractmethod
    async def get_balance(self) -> dict[str, Any]:
        """Fetches the account balance, returning a dictionary with fiat and crypto balances."""
        pass

    @abstractmethod
    async def place_order(
        self,
        pair: str,
        order_side: str,
        order_type: str,
        amount: float,
        price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, str | float]:
        """Places an order, returning a dictionary with order details including id and status.

        Args:
            pair: Trading pair symbol (e.g., "BTC/USDT").
            order_side: Order side as expected by CCXT — "buy" or "sell".
            order_type: Order type as expected by CCXT — "market" or "limit".
            amount: Order quantity in base currency.
            price: Limit price in quote currency. Required for limit orders, None for market orders.
            client_order_id: Id the exchange stores with the order, so an order whose answer was lost can be found.
        """
        pass

    @abstractmethod
    async def fetch_order_by_client_id(self, client_order_id: str, pair: str) -> dict[str, str | float]:
        """Fetches the order placed under `client_order_id`; raises OrderNotFoundError when there is none."""
        pass

    @abstractmethod
    def round_amount(self, pair: str, amount: float) -> float:
        """`amount` at the exchange's precision for `pair`."""
        pass

    @abstractmethod
    def round_price(self, pair: str, price: float) -> float:
        """`price` at the exchange's precision for `pair`."""
        pass

    @abstractmethod
    def fetch_ohlcv(
        self,
        pair: str,
        timeframe: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """
        Fetches historical OHLCV data as a list of dictionaries, each containing open, high, low,
        close, and volume for the specified time period.
        """
        pass

    @abstractmethod
    async def get_current_price(
        self,
        pair: str,
    ) -> float:
        """Fetches the current market price for the specified trading pair."""
        pass

    @abstractmethod
    async def cancel_order(
        self,
        order_id: str,
        pair: str,
    ) -> dict[str, str | float]:
        """Attempts to cancel an order by ID, returning the result of the cancellation."""
        pass

    @abstractmethod
    async def get_exchange_status(self) -> dict:
        """Fetches current exchange status."""
        pass

    @abstractmethod
    async def fetch_open_orders(self, pair: str) -> list[dict[str, Any]]:
        """Fetches all open orders for the given trading pair from the exchange."""
        pass

    @abstractmethod
    async def close_connection(self) -> None:
        """Close current exchange connection."""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Releases the exchange connection; called once, last, by the bot's shutdown."""
        pass
