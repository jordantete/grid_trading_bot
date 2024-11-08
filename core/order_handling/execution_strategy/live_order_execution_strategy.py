import time, logging, asyncio
from typing import Optional
from ..order import OrderType, OrderSide
from core.services.exchange_interface import ExchangeInterface
from core.services.exceptions import DataFetchError
from .order_execution_strategy import OrderExecutionStrategy
from ..exceptions import OrderExecutionFailedError

class LiveOrderExecutionStrategy(OrderExecutionStrategy):
    def __init__(
        self, 
        exchange_service: ExchangeInterface, 
        max_retries: int = 3, 
        retry_delay: int = 1, 
        max_slippage: float = 0.01
    ) -> None:
        self.exchange_service = exchange_service
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.max_slippage = max_slippage
        self.logger = logging.getLogger(self.__class__.__name__)

    async def execute_market_order(
        self, 
        order_side: OrderSide, 
        pair: str, 
        quantity: float, 
        price: float
    ) -> dict:
        for attempt in range(self.max_retries):
            try:
                order = await self.exchange_service.place_order(pair, OrderType.MARKET.value.lower(), order_side.name.lower(), quantity, price)
                order_result = await self._normalize_order_result(order)
                
                if order_result['status'] == 'filled':
                    return order_result  # Order fully filled
                elif order_result['status'] == 'partially_filled':
                    await self._handle_partial_fill(order_result, pair)

                await asyncio.sleep(self.retry_delay)
                self.logger.info(f"Retrying order. Attempt {attempt + 1}/{self.max_retries}.")
                price = await self._adjust_price(order_side, price, attempt)

            except Exception as e:
                self.logger.error(f"Attempt {attempt + 1} failed with error: {str(e)}")
                await asyncio.sleep(self.retry_delay)

        raise OrderExecutionFailedError("Failed to execute Market order after maximum retries.", order_side, OrderType.MARKET, pair, quantity, price)
    
    async def execute_limit_order(
        self, 
        order_side: OrderSide, 
        pair: str, 
        quantity: float, 
        price: float
    ) -> dict:
        try:
            order = await self.exchange_service.place_order(pair, OrderType.LIMIT.value.lower(), order_side.name.lower(), quantity, price)
            order_result = await self._normalize_order_result(order)
            return order_result
        
        except DataFetchError as e:
            self.logger.error(f"DataFetchError during order execution for {pair} - {e}")
            raise OrderExecutionFailedError(f"Failed to execute Limit order on {pair}: {e}", order_side, OrderType.LIMIT, pair, quantity, price)

        except Exception as e:
            self.logger.error(f"Unexpected error in execute_limit_order: {e}")
            raise OrderExecutionFailedError(f"Unexpected error during order execution: {e}", order_side, OrderType.LIMIT, pair, quantity, price)

    async def _normalize_order_result(
        self, 
        raw_order_result: dict
    ) -> dict:
        return {
            'id': raw_order_result.get('id', ''),
            'status': raw_order_result.get('status', 'unknown'),
            'type': raw_order_result.get('type', 'unknown'),
            'side': raw_order_result.get('side', 'unknown'),
            'price': raw_order_result.get('price', 0.0),
            'filled_qty': raw_order_result.get('filled', 0.0),
            'remaining_qty': raw_order_result.get('remaining', 0.0),
            'timestamp': raw_order_result.get('timestamp', int(time.time())),
            'filled_price': raw_order_result.get('average', None),
            'fee': raw_order_result.get('fee', None),
            'cost': raw_order_result.get('cost', None)
        }

    async def _adjust_price(
        self, 
        order_side: OrderSide, 
        price: float, 
        attempt: int
    ) -> float:
        adjustment = self.max_slippage / self.max_retries * attempt
        return price * (1 + adjustment) if order_side == OrderSide.BUY else price * (1 - adjustment)
    
    async def _handle_partial_fill(
        self, 
        order_result: dict, 
        pair: str,
    ) -> Optional[dict]:
        filled_qty = order_result.get('filled_qty', 0)
        self.logger.info(f"Order partially filled with {filled_qty}. Attempting to cancel and retry full quantity.")

        if not await self._retry_cancel_order(order_result['id'], pair):
            self.logger.error(f"Unable to cancel partially filled order {order_result['id']} after retries.")

    async def _retry_cancel_order(
        self, 
        order_id: str, 
        pair: str
    ) -> bool:
        for cancel_attempt in range(self.max_retries):
            try:
                cancel_result = await self.exchange_service.cancel_order(order_id, pair)

                if cancel_result['status'] == 'canceled':
                    self.logger.info(f"Successfully canceled order {order_id}.")
                    return True

                self.logger.warning(f"Cancel attempt {cancel_attempt + 1} for order {order_id} failed.")

            except Exception as e:
                self.logger.warning(f"Error during cancel attempt {cancel_attempt + 1} for order {order_id}: {str(e)}")

            await asyncio.sleep(self.retry_delay)
        return False


"""
Order Result Structure:

order_result = {
    'id': str,                 # Unique identifier for the order provided by the exchange.
    'status': str,             # Status of the order, e.g., 'filled', 'partially_filled', 'open', 'canceled'.
    'type': str,               # Order type, e.g., 'limit', 'market'.
    'side': str,               # Side of the order, e.g., 'buy' or 'sell'.
    'price': float,            # Executed price of the order. For partially filled orders, this could be the average fill price.
    'filled_qty': float,       # Quantity of the order that has been filled. Should match `quantity` if fully filled.
    'remaining_qty': float,    # Quantity still remaining to be filled.
    'timestamp': int or str,   # Timestamp when the order was placed.
    # Optional Fields:
    'filled_price': float,     # The average price of the filled portion (if the API provides it separately).
    'fee': float,              # Total fee incurred for the order (if available).
    'cost': float              # Total cost of the order execution.
}
"""