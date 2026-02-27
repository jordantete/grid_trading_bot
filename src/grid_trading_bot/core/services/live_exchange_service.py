import asyncio
from collections.abc import Callable
import logging
import math
import os
from typing import Any

from ccxt.base.errors import BaseError, ExchangeError, NetworkError, OrderNotFound
import ccxt.pro as ccxtpro
import pandas as pd

from grid_trading_bot.config.config_manager import ConfigManager

from .circuit_breaker import CircuitBreaker
from .exceptions import (
    CircuitBreakerOpenError,
    DataFetchError,
    MissingEnvironmentVariableError,
    OrderCancellationError,
    UnsupportedExchangeError,
)
from .exchange_interface import ExchangeInterface


class LiveExchangeService(ExchangeInterface):
    def __init__(
        self,
        config_manager: ConfigManager,
        is_paper_trading_activated: bool,
    ):
        self.config_manager = config_manager
        self.is_paper_trading_activated = is_paper_trading_activated
        self.logger = logging.getLogger(self.__class__.__name__)
        self.exchange_name = self.config_manager.get_exchange_name()
        self.api_key = self._get_env_variable("EXCHANGE_API_KEY")
        self.secret_key = self._get_env_variable("EXCHANGE_SECRET_KEY")
        self.exchange = self._initialize_exchange()
        self.connection_active = False
        self.websocket_max_retries: int = self.config_manager.get_websocket_max_retries()
        self.websocket_retry_base_delay: int = self.config_manager.get_websocket_retry_base_delay()
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=self.config_manager.get_circuit_breaker_failure_threshold(),
            recovery_timeout=self.config_manager.get_circuit_breaker_recovery_timeout(),
            half_open_max_calls=self.config_manager.get_circuit_breaker_half_open_max_calls(),
        )
        self._last_known_price: float | None = None
        self._max_price_deviation: float = 0.50

    def _get_env_variable(self, key: str) -> str:
        value = os.getenv(key)
        if value is None:
            raise MissingEnvironmentVariableError(f"Missing required environment variable: {key}")
        if not value.strip():
            raise MissingEnvironmentVariableError(f"Environment variable {key} is empty")
        return value.strip()

    def _initialize_exchange(self) -> Any:
        try:
            exchange = getattr(ccxtpro, self.exchange_name)(
                {
                    "apiKey": self.api_key,
                    "secret": self.secret_key,
                    "enableRateLimit": True,
                },
            )

            if self.is_paper_trading_activated:
                self._enable_sandbox_mode(exchange)
            return exchange
        except AttributeError:
            raise UnsupportedExchangeError(f"The exchange '{self.exchange_name}' is not supported.") from None

    def _enable_sandbox_mode(self, exchange: Any) -> None:
        if self.exchange_name == "binance":
            exchange.urls["api"] = "https://testnet.binance.vision/api"
        elif self.exchange_name == "kraken":
            exchange.urls["api"] = "https://api.demo-futures.kraken.com"
        elif self.exchange_name == "bitmex":
            exchange.urls["api"] = "https://testnet.bitmex.com"
        elif self.exchange_name == "bybit":
            exchange.set_sandbox_mode(True)
        else:
            self.logger.warning(f"No sandbox mode available for {self.exchange_name}. Running in live mode.")

    def _validate_price(self, price_value: Any, pair: str) -> float | None:
        """
        Validates a price value from exchange data.

        Returns the validated price as a float, or None if invalid.
        """
        if price_value is None:
            self.logger.warning(f"Received None price for {pair}.")
            return None

        try:
            price = float(price_value)
        except (TypeError, ValueError):
            self.logger.warning(f"Received non-numeric price for {pair}: {price_value}")
            return None

        if not math.isfinite(price):
            self.logger.warning(f"Received non-finite price for {pair}: {price}")
            return None

        if price <= 0:
            self.logger.warning(f"Received non-positive price for {pair}: {price}")
            return None

        if self._last_known_price is not None:
            deviation = abs(price - self._last_known_price) / self._last_known_price
            if deviation > self._max_price_deviation:
                self.logger.warning(
                    f"Price {price} for {pair} deviates {deviation:.1%} from last known price "
                    f"{self._last_known_price} (max {self._max_price_deviation:.0%}). Rejecting.",
                )
                return None

        self._last_known_price = price
        return price

    async def _subscribe_to_ticker_updates(
        self,
        pair: str,
        on_ticker_update: Callable[[float], None],
        update_interval: float,
    ) -> None:
        self.connection_active = True
        retry_count = 0

        while self.connection_active:
            try:
                ticker = await self.exchange.watch_ticker(pair)
                current_price = self._validate_price(ticker.get("last"), pair)

                if current_price is None:
                    continue

                self.logger.info(f"Connected to WebSocket for {pair} ticker current price: {current_price}")

                if not self.connection_active:
                    break

                await on_ticker_update(current_price)
                await asyncio.sleep(update_interval)
                retry_count = 0  # Reset retry count after a successful operation

            except (NetworkError, ExchangeError) as e:
                retry_count += 1
                retry_interval = min(retry_count * self.websocket_retry_base_delay, 60)
                self.logger.error(
                    f"Error connecting to WebSocket for {pair}: {e}. "
                    f"Retrying in {retry_interval} seconds ({retry_count}/{self.websocket_max_retries}).",
                )

                if retry_count >= self.websocket_max_retries:
                    self.logger.error("Max retries reached. Stopping WebSocket connection.")
                    self.connection_active = False
                    break

                await asyncio.sleep(retry_interval)

            except asyncio.CancelledError:
                self.logger.error(f"WebSocket subscription for {pair} was cancelled.")
                self.connection_active = False
                break

            except Exception as e:
                self.logger.error(f"WebSocket connection error: {e}. Reconnecting...")
                await asyncio.sleep(5)

            finally:
                if not self.connection_active:
                    try:
                        self.logger.info("Connection to Websocket no longer active.")
                        await self.exchange.close()

                    except Exception as e:
                        self.logger.error(f"Error while closing WebSocket connection: {e}", exc_info=True)

    async def listen_to_ticker_updates(
        self,
        pair: str,
        on_price_update: Callable[[float], None],
        update_interval: float,
    ) -> None:
        await self._subscribe_to_ticker_updates(pair, on_price_update, update_interval)

    async def close_connection(self) -> None:
        self.connection_active = False
        self.logger.info("Closing WebSocket connection...")

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
            validated_price = self._validate_price(ticker.get("last"), pair)

            if validated_price is None:
                raise DataFetchError(f"Invalid price received for {pair}: {ticker.get('last')}")

            return validated_price

        except CircuitBreakerOpenError as e:
            raise DataFetchError(f"Circuit breaker open: {e!s}") from e

        except BaseError as e:
            raise DataFetchError(f"Error fetching current price: {e!s}") from e

    async def place_order(
        self,
        pair: str,
        order_type: str,
        order_side: str,
        amount: float,
        price: float | None = None,
    ) -> dict[str, str | float]:
        try:
            order = await self.circuit_breaker.call(
                self.exchange.create_order, pair, order_type, order_side, amount, price
            )
            return order

        except CircuitBreakerOpenError as e:
            raise DataFetchError(f"Circuit breaker open: {e!s}") from e

        except NetworkError as e:
            raise DataFetchError(f"Network issue occurred while placing order: {e!s}") from e

        except BaseError as e:
            raise DataFetchError(f"Error placing order: {e!s}") from e

    async def fetch_order(
        self,
        order_id: str,
        pair: str,
    ) -> dict[str, str | float]:
        try:
            return await self.circuit_breaker.call(self.exchange.fetch_order, order_id, pair)

        except CircuitBreakerOpenError as e:
            raise DataFetchError(f"Circuit breaker open: {e!s}") from e

        except NetworkError as e:
            raise DataFetchError(f"Network issue occurred while fetching order status: {e!s}") from e

        except BaseError as e:
            raise DataFetchError(f"Exchange-specific error occurred: {e!s}") from e

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

        except OrderNotFound:
            raise OrderCancellationError(
                f"Order {order_id} not found for cancellation. It may already be completed or canceled.",
            ) from None

        except NetworkError as e:
            raise OrderCancellationError(f"Network error while canceling order {order_id}: {e!s}") from e

        except BaseError as e:
            raise OrderCancellationError(f"Exchange error while canceling order {order_id}: {e!s}") from e

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
