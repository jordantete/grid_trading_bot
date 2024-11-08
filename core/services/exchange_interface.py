from abc import ABC, abstractmethod
from typing import Dict, Union, Optional, Any
import pandas as pd

class ExchangeInterface(ABC):
    @abstractmethod
    async def get_balance(self) -> Dict[str, Any]:
        """Fetches the account balance, returning a dictionary with fiat and crypto balances."""
        pass
    
    @abstractmethod
    async def place_order(
        self, 
        pair: str, 
        order_side: str, 
        order_type: str, 
        amount: float, 
        price: Optional[float] = None
    ) -> Dict[str, Union[str, float]]:
        """Places an order, returning a dictionary with order details including id and status."""
        pass
    
    @abstractmethod
    def fetch_ohlcv(
        self, 
        pair: str, 
        timeframe: str, 
        start_date: str, 
        end_date: str
    ) -> pd.DataFrame:
        """
        Fetches historical OHLCV data as a list of dictionaries, each containing open, high, low,
        close, and volume for the specified time period.
        """
        pass
    
    @abstractmethod
    async def get_current_price(
        self, 
        pair: str
    ) -> float:
        """Fetches the current market price for the specified trading pair."""
        pass

    @abstractmethod
    async def get_order_status(
        self, 
        order_id: str
    ) -> Dict[str, Union[str, float]]:
        """Fetches the status of an order by its ID, returning details such as status and filled quantity."""
        pass

    @abstractmethod
    async def cancel_order(
        self, 
        order_id: str, 
        pair: str
    ) -> Dict[str, Union[str, float]]:
        """Attempts to cancel an order by ID, returning the result of the cancellation."""
        pass

    @abstractmethod
    async def get_exchange_status(self) -> dict:
        """Fetches current exchange status."""
        pass
