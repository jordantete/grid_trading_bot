from collections.abc import Awaitable, Callable
import logging
import os
from typing import Any

from ccxt.base.errors import (
    BadRequest,
    BaseError,
    DuplicateOrderId,
    InsufficientFunds,
    InvalidOrder,
    NetworkError,
    OrderImmediatelyFillable,
    OrderNotFound,
)
import ccxt.pro as ccxtpro
import pandas as pd

from grid_trading_bot.config.config_manager import ConfigManager

from .circuit_breaker import CircuitBreaker
from .exceptions import (
    CircuitBreakerOpenError,
    DataFetchError,
    MissingEnvironmentVariableError,
    OrderCancellationError,
    OrderNotFoundError,
    OrderNotPlacedError,
    OrderOutcomeUnknownError,
    OrderRejectedError,
    PostOnlyRejectedError,
    UnsupportedExchangeError,
)
from .exchange_interface import ExchangeInterface
from .live_ticker_feed import LiveTickerFeed
from .market_constraints import MarketConstraints

# Venues that do not map post-only rejections onto ccxt's OrderImmediatelyFillable report
# them as a plain InvalidOrder, so the wording is the only signal left (e.g. Kraken's
# "EOrder:Post only order").
_POST_ONLY_REJECTION_MARKERS = (
    "post only",
    "postonly",
    "immediately match",
    "immediately fillable",
)


def _is_post_only_rejection(error: BaseException) -> bool:
    """Whether a ccxt order error means "this maker-only order would have crossed"."""
    if isinstance(error, OrderImmediatelyFillable):
        return True
    if not isinstance(error, InvalidOrder):
        return False
    message = str(error).lower().replace("-", " ").replace("_", " ")
    return any(marker in message for marker in _POST_ONLY_REJECTION_MARKERS)


def _is_duplicate_client_id(error: BaseException) -> bool:
    """Whether a plain InvalidOrder says the client order id is already held (Binance -2010/-4116, OKX 51016)."""
    return isinstance(error, InvalidOrder) and "duplicat" in str(error).lower()


class LiveExchangeService(ExchangeInterface):
    def __init__(
        self,
        config_manager: ConfigManager,
        is_paper_trading_activated: bool,
    ):
        self.config_manager = config_manager
        self.is_paper_trading_activated = is_paper_trading_activated
        self.logger = logging.getLogger(self.__class__.__name__)
        self.exchange_name = self.config_manager.config.exchange.name
        self.api_key = self._get_env_variable("EXCHANGE_API_KEY")
        self.secret_key = self._get_env_variable("EXCHANGE_SECRET_KEY")
        self.exchange = self._initialize_exchange()
        self._exchange_closed = False
        self.post_only: bool = self.config_manager.config.exchange.post_only
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=self.config_manager.config.execution.circuit_breaker_failure_threshold,
            recovery_timeout=self.config_manager.config.execution.circuit_breaker_recovery_timeout,
            half_open_max_calls=self.config_manager.config.execution.circuit_breaker_half_open_max_calls,
            # A refused order, an order the exchange no longer knows and a client id it already holds are normal
            # answers, not exchange faults, so a run of them must never open the breaker.
            benign_exceptions=(OrderRejectedError, OrderNotFound, DuplicateOrderId),
        )
        self.ticker_feed = LiveTickerFeed(
            self.exchange,
            websocket_max_retries=self.config_manager.config.execution.websocket_max_retries,
            websocket_retry_base_delay=self.config_manager.config.execution.websocket_retry_base_delay,
        )

    def _get_env_variable(self, key: str) -> str:
        value = os.getenv(key)
        if value is None:
            raise MissingEnvironmentVariableError(f"Missing required environment variable: {key}")
        if not value.strip():
            raise MissingEnvironmentVariableError(f"Environment variable {key} is empty")
        return value.strip()

    def _initialize_exchange(self) -> Any:
        try:
            exchange_class = getattr(ccxtpro, self.exchange_name)
        except AttributeError:
            raise UnsupportedExchangeError(f"The exchange '{self.exchange_name}' is not supported.") from None
        exchange = exchange_class(
            {
                "apiKey": self.api_key,
                "secret": self.secret_key,
                "enableRateLimit": True,
            },
        )
        if exchange.requiredCredentials.get("password") is True:
            exchange.password = self._get_env_variable("EXCHANGE_PASSWORD")
        if self.is_paper_trading_activated:
            self._enable_sandbox_mode(exchange)
        return exchange

    def _enable_sandbox_mode(self, exchange: Any) -> None:
        if exchange.has.get("sandbox") is not True:
            raise UnsupportedExchangeError(f"{self.exchange_name} has no test environment; paper trading refused.")
        exchange.set_sandbox_mode(True)

    @property
    def last_price(self) -> float | None:
        """Last price accepted by the ticker feed's deviation filter, None before the first one."""
        return self.ticker_feed.last_price

    async def listen_to_ticker_updates(
        self,
        pair: str,
        on_price_update: Callable[[float], Awaitable[None]],
        update_interval: float,
    ) -> None:
        await self.ticker_feed.subscribe(pair, on_price_update, update_interval)

    async def close_connection(self) -> None:
        """Ends the ticker loop; the exchange stays open for the shutdown's REST calls."""
        self.ticker_feed.stop()

    async def close(self) -> None:
        """Closes the ccxt instance once; the bot calls it last during its shutdown."""
        await self.close_connection()
        if self._exchange_closed:
            return
        self._exchange_closed = True
        try:
            await self.exchange.close()
            self.logger.info("Exchange connection closed.")
        except Exception as e:
            self.logger.error(f"Error while closing the exchange connection: {e}", exc_info=True)

    async def get_balance(self) -> dict[str, Any]:
        try:
            return await self.circuit_breaker.call(self.exchange.fetch_balance)

        except CircuitBreakerOpenError as e:
            raise DataFetchError(f"Circuit breaker open: {e!s}") from e

        except BaseError as e:
            raise DataFetchError(f"Error fetching balance: {e!s}") from e

    async def get_current_price(self, pair: str) -> float:
        try:
            ticker = await self.circuit_breaker.call(self.exchange.fetch_ticker, pair)
            validated_price = self.ticker_feed.validate_price(ticker.get("last"), pair)

            if validated_price is None:
                raise DataFetchError(f"Invalid price received for {pair}: {ticker.get('last')}")

            return validated_price

        except CircuitBreakerOpenError as e:
            raise DataFetchError(f"Circuit breaker open: {e!s}") from e

        except BaseError as e:
            raise DataFetchError(f"Error fetching current price: {e!s}") from e

    @property
    def last_price_update_ts(self) -> float | None:
        return self.ticker_feed.last_price_update_ts

    async def load_exchange_markets(self, pair: str) -> None:
        try:
            await self.circuit_breaker.call(self.exchange.load_markets)
        except CircuitBreakerOpenError as e:
            raise DataFetchError(f"Circuit breaker open: {e!s}") from e
        except BaseError as e:
            raise DataFetchError(f"Failed to load markets for {self.exchange_name}: {e!s}") from e

        if pair not in (self.exchange.markets or {}):
            raise DataFetchError(f"Pair {pair} is not available on {self.exchange_name}.")
        self.logger.info(f"Loaded {len(self.exchange.markets)} markets from {self.exchange_name}.")

    def get_market_constraints(self, pair: str) -> MarketConstraints | None:
        market = (self.exchange.markets or {}).get(pair)
        if market is None:
            raise DataFetchError(f"Market metadata for {pair} not loaded. Call load_exchange_markets first.")
        limits = market.get("limits") or {}
        return MarketConstraints(
            min_amount=(limits.get("amount") or {}).get("min"),
            min_cost=(limits.get("cost") or {}).get("min"),
        )

    def _order_params(self, order_type: str, client_order_id: str | None = None) -> dict[str, Any]:
        """
        Builds the exchange-specific params for an order.

        `postOnly` is only ever set on limit orders: a market order crosses the book by
        definition, and exchanges reject the combination. When `exchange.post_only` is
        enabled the exchange rejects a limit order that would cross instead of filling it
        as a taker; that rejection surfaces as `PostOnlyRejectedError` and leaves the grid
        level untouched for a later cycle rather than counting as a failed placement.
        `clientOrderId` lets the bot find the order again when the answer to the placement is lost.
        """
        params: dict[str, Any] = {}
        if self.post_only and order_type.lower() == "limit":
            params["postOnly"] = True
        if client_order_id is not None:
            params["clientOrderId"] = client_order_id
        return params

    def round_amount(self, pair: str, amount: float) -> float:
        try:
            return float(self.exchange.amount_to_precision(pair, amount))
        except InvalidOrder as e:
            raise OrderRejectedError(f"Amount {amount} is under the precision of {pair}: {e!s}") from e

    def round_price(self, pair: str, price: float) -> float:
        return float(self.exchange.price_to_precision(pair, price))

    async def place_order(
        self,
        pair: str,
        order_type: str,
        order_side: str,
        amount: float,
        price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, str | float]:
        """
        Sends one order. Raises `PostOnlyRejectedError` or `OrderRejectedError` when the exchange refuses it,
        `OrderNotPlacedError` when the breaker blocked the request and `OrderOutcomeUnknownError` for every other
        failure, since the order may be live (network error, unknown execution status, empty answer, a client id
        the exchange already holds).
        """
        try:
            precise_amount = self.round_amount(pair, amount)
            precise_price = self.round_price(pair, price) if price is not None else None
            params = self._order_params(order_type, client_order_id)

            async def create_order() -> dict[str, str | float]:
                # Refusals are translated inside the breaker's callable so the breaker sees its benign types
                # rather than the raw ccxt errors it would otherwise count as failures.
                try:
                    return await self.exchange.create_order(
                        pair, order_type, order_side, precise_amount, precise_price, params=params
                    )
                except (DuplicateOrderId, OrderNotFound):
                    raise
                except BaseError as e:
                    if self.post_only and _is_post_only_rejection(e):
                        raise PostOnlyRejectedError(
                            f"Maker-only {order_side} order on {pair} at {precise_price} would have crossed: {e!s}",
                        ) from e
                    if client_order_id is not None and _is_duplicate_client_id(e):
                        raise DuplicateOrderId(str(e)) from e
                    if isinstance(e, InsufficientFunds | InvalidOrder | BadRequest):
                        raise OrderRejectedError(f"The exchange refused the {order_side} order on {pair}: {e!s}") from e
                    raise

            return await self.circuit_breaker.call(create_order)

        except CircuitBreakerOpenError as e:
            raise OrderNotPlacedError(f"Circuit breaker open: {e!s}") from e

        except DuplicateOrderId as e:
            raise OrderOutcomeUnknownError(
                f"The exchange already holds client order id {client_order_id} on {pair}: {e!s}"
            ) from e

        except NetworkError as e:
            raise OrderOutcomeUnknownError(f"Network issue occurred while placing order: {e!s}") from e

        except BaseError as e:
            raise OrderOutcomeUnknownError(f"Error placing order, the outcome is unknown: {e!s}") from e

    async def fetch_order(
        self,
        order_id: str,
        pair: str,
    ) -> dict[str, str | float]:
        try:
            return await self.circuit_breaker.call(self.exchange.fetch_order, order_id, pair)

        except CircuitBreakerOpenError as e:
            raise DataFetchError(f"Circuit breaker open: {e!s}") from e

        except OrderNotFound as e:
            raise OrderNotFoundError(f"Order {order_id} not found on {pair}: {e!s}") from e

        except NetworkError as e:
            raise DataFetchError(f"Network issue occurred while fetching order status: {e!s}") from e

        except BaseError as e:
            raise DataFetchError(f"Exchange-specific error occurred: {e!s}") from e

    async def fetch_order_by_client_id(self, client_order_id: str, pair: str) -> dict[str, str | float]:
        try:
            return await self.circuit_breaker.call(
                self.exchange.fetch_order, None, pair, params={"clientOrderId": client_order_id}
            )

        except CircuitBreakerOpenError as e:
            raise DataFetchError(f"Circuit breaker open: {e!s}") from e

        except OrderNotFound as e:
            raise OrderNotFoundError(f"No order with client order id {client_order_id} on {pair}: {e!s}") from e

        except BaseError as e:
            raise DataFetchError(f"Error looking up client order id {client_order_id}: {e!s}") from e

    async def cancel_order(
        self,
        order_id: str,
        pair: str,
    ) -> dict:
        try:
            self.logger.info(f"Attempting to cancel order {order_id} for pair {pair}")
            cancellation_result = await self.circuit_breaker.call(self.exchange.cancel_order, order_id, pair)

            if cancellation_result["status"] in ["canceled", "closed"]:
                self.logger.info(f"Order {order_id} successfully canceled.")
                return cancellation_result
            else:
                self.logger.warning(f"Order {order_id} cancellation status: {cancellation_result['status']}")
                return cancellation_result

        except CircuitBreakerOpenError as e:
            raise OrderCancellationError(f"Circuit breaker open: {e!s}") from e

        except OrderNotFound as e:
            raise OrderNotFoundError(f"Order {order_id} not found for cancellation on {pair}: {e!s}") from e

        except NetworkError as e:
            raise OrderCancellationError(f"Network error while canceling order {order_id}: {e!s}") from e

        except BaseError as e:
            raise OrderCancellationError(f"Exchange error while canceling order {order_id}: {e!s}") from e

    async def fetch_open_orders(self, pair: str) -> list[dict[str, Any]]:
        try:
            return await self.circuit_breaker.call(self.exchange.fetch_open_orders, pair)

        except CircuitBreakerOpenError as e:
            raise DataFetchError(f"Circuit breaker open: {e!s}") from e

        except BaseError as e:
            raise DataFetchError(f"Error fetching open orders: {e!s}") from e

    async def get_exchange_status(self) -> dict:
        try:
            status = await self.exchange.fetch_status()
            return {
                "status": status.get("status", "unknown"),
                "updated": status.get("updated"),
                "eta": status.get("eta"),
                "url": status.get("url"),
                "info": status.get("info", "No additional info available"),
            }

        except AttributeError:
            return {"status": "unsupported", "info": "fetch_status not supported by this exchange."}

        except Exception as e:
            return {"status": "error", "info": f"Failed to fetch exchange status: {e}"}

    def fetch_ohlcv(
        self,
        pair: str,
        timeframe: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        raise NotImplementedError("fetch_ohlcv is not used in live or paper trading mode.")

    async def fetch_recent_ohlcv(self, pair: str, timeframe: str, limit: int) -> pd.DataFrame:
        try:
            candles = await self.circuit_breaker.call(self.exchange.fetch_ohlcv, pair, timeframe, None, limit)
            df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            return df

        except CircuitBreakerOpenError as e:
            raise DataFetchError(f"Circuit breaker open: {e!s}") from e

        except BaseError as e:
            raise DataFetchError(f"Failed to fetch recent OHLCV for {pair}: {e!s}") from e
