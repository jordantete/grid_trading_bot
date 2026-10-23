import asyncio
from collections.abc import Awaitable, Callable
import logging
import math
import time
from typing import Any

from ccxt.base.errors import ExchangeError, NetworkError

from .exceptions import DataFetchError


class LiveTickerFeed:
    """Streams validated ticker prices from an exchange websocket and keeps the last accepted price."""

    def __init__(
        self,
        exchange: Any,
        max_price_deviation: float,
        max_consecutive_price_rejections: int,
        websocket_max_retries: int,
        websocket_retry_base_delay: int,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.exchange = exchange
        self.websocket_max_retries = websocket_max_retries
        self.websocket_retry_base_delay = websocket_retry_base_delay
        self.connection_active = False
        self._closing = False
        self._max_price_deviation = max_price_deviation
        self._max_consecutive_price_rejections = max_consecutive_price_rejections
        self._consecutive_price_rejections = 0
        self._last_known_price: float | None = None
        self._last_price_update_ts: float | None = None

    @property
    def last_price(self) -> float | None:
        """Last price accepted by the deviation filter, None before the first one."""
        return self._last_known_price

    @property
    def last_price_update_ts(self) -> float | None:
        return self._last_price_update_ts

    def validate_price(self, price_value: Any, pair: str) -> float | None:
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
                self._consecutive_price_rejections += 1
                if self._consecutive_price_rejections < self._max_consecutive_price_rejections:
                    self.logger.warning(
                        f"Price {price} for {pair} deviates {deviation:.1%} from last known price "
                        f"{self._last_known_price} (max {self._max_price_deviation:.0%}). Rejecting "
                        f"({self._consecutive_price_rejections}/{self._max_consecutive_price_rejections}).",
                    )
                    return None
                self.logger.warning(
                    f"Accepting price {price} for {pair} after "
                    f"{self._consecutive_price_rejections} consecutive deviation rejections — re-anchoring.",
                )

        self._consecutive_price_rejections = 0
        self._last_known_price = price
        self._last_price_update_ts = time.monotonic()
        return price

    async def subscribe(
        self,
        pair: str,
        on_ticker_update: Callable[[float], Awaitable[None]],
        update_interval: float,
    ) -> None:
        if self._closing:
            return
        self.connection_active = True
        retry_count = 0

        while self.connection_active and not self._closing:
            try:
                ticker = await self.exchange.watch_ticker(pair)
                current_price = self.validate_price(ticker.get("last"), pair)

                if current_price is None:
                    continue

                self.logger.info(f"Connected to WebSocket for {pair} ticker current price: {current_price}")

                if not self.connection_active:
                    break

                await on_ticker_update(current_price)
                await asyncio.sleep(update_interval)
                retry_count = 0  # Reset retry count after a successful operation

            except (NetworkError, ExchangeError) as e:
                if self._closing:
                    break
                retry_count += 1
                retry_interval = min(retry_count * self.websocket_retry_base_delay, 60)
                self.logger.error(
                    f"Error connecting to WebSocket for {pair}: {e}. "
                    f"Retrying in {retry_interval} seconds ({retry_count}/{self.websocket_max_retries}).",
                )

                if retry_count >= self.websocket_max_retries:
                    self.logger.error("Max retries reached. Stopping WebSocket connection.")
                    self.connection_active = False
                    raise DataFetchError(
                        f"WebSocket feed for {pair} failed after {retry_count} retries: {e!s}",
                    ) from e

                await asyncio.sleep(retry_interval)

            except asyncio.CancelledError:
                self.logger.error(f"WebSocket subscription for {pair} was cancelled.")
                self.connection_active = False
                break

            except Exception as e:
                if self._closing:
                    break
                retry_count += 1
                retry_interval = min(retry_count * self.websocket_retry_base_delay, 60)
                self.logger.error(
                    f"WebSocket connection error: {e}. Retrying in {retry_interval} seconds "
                    f"({retry_count}/{self.websocket_max_retries}).",
                )
                if retry_count >= self.websocket_max_retries:
                    self.connection_active = False
                    raise DataFetchError(
                        f"WebSocket feed for {pair} failed after {retry_count} retries: {e!s}",
                    ) from e
                await asyncio.sleep(retry_interval)

    def stop(self) -> None:
        """Ends the subscription loop for good; the exchange is left open."""
        self._closing = True
        self.connection_active = False
        self.logger.info("Stopping the ticker feed...")
