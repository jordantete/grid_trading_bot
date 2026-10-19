import asyncio
import logging

from grid_trading_bot.core.services.exceptions import (
    DataFetchError,
    OrderCancellationError,
    OrderNotFoundError,
    OrderNotPlacedError,
    OrderOutcomeUnknownError,
    OrderRejectedError,
)
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface

from ..order import Order, OrderSide, OrderStatus, OrderType
from .order_execution_strategy_interface import OrderExecutionStrategyInterface


class LiveOrderExecutionStrategy(OrderExecutionStrategyInterface):
    LOOKUP_ATTEMPTS = 3
    LOOKUP_DELAY = 1.0  # seconds

    def __init__(
        self,
        exchange_service: ExchangeInterface,
        max_retries: int = 3,
        retry_delay: int = 1,
    ) -> None:
        self.exchange_service = exchange_service
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.logger = logging.getLogger(self.__class__.__name__)

    async def execute_market_order(
        self,
        order_side: OrderSide,
        pair: str,
        quantity: float,
        price: float,
        client_order_id: str,
    ) -> Order:
        """
        Sends one market leg and returns it closed with what it executed. A leg the exchange leaves open is
        cancelled first, and raises OrderOutcomeUnknownError when it cannot be cancelled or its final state cannot be
        read; a leg that executed nothing raises.
        """
        leg = await self._send(OrderType.MARKET, order_side, pair, quantity, price, client_order_id)
        if leg.status == OrderStatus.OPEN:
            settled = await self._handle_partial_fill(leg, pair)
            if settled is None:
                raise OrderOutcomeUnknownError(
                    f"Market {order_side.value} order {leg.identifier} is still open on the exchange and could not "
                    f"be cancelled."
                )
            if settled.status == OrderStatus.OPEN:
                raise OrderOutcomeUnknownError(
                    f"Market {order_side.value} order {leg.identifier} was cancelled but its final state could not "
                    f"be read."
                )
            leg = settled

        if leg.filled is not None:
            filled = leg.filled
        elif leg.status == OrderStatus.CLOSED:
            filled = leg.amount
        else:
            filled = 0.0
        if filled <= 0 and leg.status == OrderStatus.REJECTED:
            raise OrderRejectedError(f"The exchange rejected market {order_side.value} order {leg.identifier}.")
        if filled <= 0 and leg.status != OrderStatus.CLOSED:
            raise OrderNotPlacedError(
                f"Market {order_side.value} order {leg.identifier} ended {leg.status.value} without a fill."
            )

        if leg.average is not None:
            leg_price = leg.average
        elif leg.price is not None:
            leg_price = leg.price
        else:
            leg_price = price
        return self._closed_leg(leg, quantity, filled, filled * leg_price)

    def _closed_leg(
        self,
        leg: Order,
        requested_quantity: float,
        filled: float,
        cost: float,
    ) -> Order:
        """The closed view of one market leg: `filled` executed, the rest of `requested_quantity` did not."""
        average = cost / filled if filled > 0 else leg.average
        return Order(
            identifier=leg.identifier,
            status=OrderStatus.CLOSED,
            order_type=OrderType.MARKET,
            side=leg.side,
            price=leg.price,
            average=average,
            amount=requested_quantity,
            filled=filled,
            remaining=max(requested_quantity - filled, 0.0),
            timestamp=leg.timestamp,
            datetime=leg.datetime,
            last_trade_timestamp=leg.last_trade_timestamp,
            symbol=leg.symbol,
            time_in_force=leg.time_in_force,
            trades=leg.trades,
            fee=leg.fee,
            cost=cost,
            info=leg.info,
        )

    async def execute_limit_order(
        self,
        order_side: OrderSide,
        pair: str,
        quantity: float,
        price: float,
        client_order_id: str,
    ) -> Order:
        return await self._send(OrderType.LIMIT, order_side, pair, quantity, price, client_order_id)

    async def _send(
        self,
        order_type: OrderType,
        order_side: OrderSide,
        pair: str,
        quantity: float,
        price: float,
        client_order_id: str,
    ) -> Order:
        """
        Places one order under `client_order_id`. When the answer is lost or unreadable, the order is looked up by
        that id before anything is reported.
        """
        try:
            raw_order = await self.exchange_service.place_order(
                pair,
                order_type.value,
                order_side.value,
                quantity,
                price,
                client_order_id=client_order_id,
            )
        except OrderOutcomeUnknownError as error:
            self.logger.warning(f"No answer for {order_type.value} order {client_order_id} ({error}); looking it up.")
            return await self._find_after_lost_answer(client_order_id, pair)
        try:
            return await self._parse_order_result(raw_order)
        except DataFetchError as error:
            self.logger.warning(f"Unreadable answer for order {client_order_id} ({error}); looking it up.")
            return await self._find_after_lost_answer(client_order_id, pair)

    async def _find_after_lost_answer(self, client_order_id: str, pair: str) -> Order:
        """
        Looks the order up by `client_order_id` up to LOOKUP_ATTEMPTS times. Raises OrderNotPlacedError when every
        lookup says it does not exist, OrderOutcomeUnknownError when a lookup failed and none found it.
        """
        lookup_failed = False
        for _ in range(self.LOOKUP_ATTEMPTS):
            await asyncio.sleep(self.LOOKUP_DELAY)
            try:
                order = await self.find_order(client_order_id, pair)
            except DataFetchError as error:
                self.logger.warning(f"Lookup of order {client_order_id} failed: {error}")
                lookup_failed = True
                continue
            if order is not None:
                self.logger.info(f"Order {client_order_id} found on the exchange as {order.identifier}.")
                return order
        if lookup_failed:
            raise OrderOutcomeUnknownError(f"Order {client_order_id} could not be confirmed or ruled out.")
        raise OrderNotPlacedError(
            f"Order {client_order_id} is not on the exchange after {self.LOOKUP_ATTEMPTS} lookups."
        )

    async def find_order(self, client_order_id: str, pair: str) -> Order | None:
        try:
            raw_order = await self.exchange_service.fetch_order_by_client_id(client_order_id, pair)
        except OrderNotFoundError:
            return None
        return await self._parse_order_result(raw_order)

    async def get_order(
        self,
        order_id: str,
        pair: str,
    ) -> Order | None:
        try:
            raw_order = await self.exchange_service.fetch_order(order_id, pair)
            order_result = await self._parse_order_result(raw_order)
            return order_result

        except DataFetchError:
            raise

    async def cancel_order(self, order_id: str, pair: str) -> bool:
        return await self._retry_cancel_order(order_id, pair)

    def round_amount(self, pair: str, amount: float) -> float:
        return self.exchange_service.round_amount(pair, amount)

    def round_price(self, pair: str, price: float) -> float:
        return self.exchange_service.round_price(pair, price)

    async def _parse_order_result(
        self,
        raw_order_result: dict,
    ) -> Order:
        """
        Parses the raw order response from the exchange into an Order object.

        Args:
            raw_order_result: The raw response from the exchange.

        Returns:
            An Order object with standardized fields.

        Raises:
            DataFetchError: If required fields are missing from the exchange response.
        """
        required_fields = ("id", "status", "type", "side")
        missing = [f for f in required_fields if not raw_order_result.get(f)]
        if missing:
            raise DataFetchError(f"Exchange response missing required fields: {', '.join(missing)}")

        return Order(
            identifier=raw_order_result["id"],
            status=OrderStatus(raw_order_result["status"].lower()),
            order_type=OrderType(raw_order_result["type"].lower()),
            side=OrderSide(raw_order_result["side"].lower()),
            price=raw_order_result.get("price", 0.0),
            average=raw_order_result.get("average"),
            amount=raw_order_result.get("amount", 0.0),
            filled=raw_order_result.get("filled", 0.0),
            remaining=raw_order_result.get("remaining", 0.0),
            timestamp=raw_order_result.get("timestamp", 0),
            datetime=raw_order_result.get("datetime"),
            last_trade_timestamp=raw_order_result.get("lastTradeTimestamp"),
            symbol=raw_order_result.get("symbol", ""),
            time_in_force=raw_order_result.get("timeInForce"),
            trades=raw_order_result.get("trades", []),
            fee=raw_order_result.get("fee"),
            cost=raw_order_result.get("cost"),
            info=raw_order_result.get("info", raw_order_result),
        )

    async def _handle_partial_fill(
        self,
        order: Order,
        pair: str,
    ) -> Order | None:
        """
        Handles a partially filled order by attempting to cancel it, then refetches it to
        capture any fill that landed between the placement response and the cancel taking effect.

        Returns:
            The authoritative Order to use for accounting (refetched, or the placement-response
            order if the refetch itself fails), or None if the cancel could not be completed.
        """
        self.logger.info(f"Order partially filled with {order.filled}. Attempting to cancel the rest.")

        if not await self._retry_cancel_order(order.identifier, pair):
            self.logger.error(f"Unable to cancel partially filled order {order.identifier} after retries.")
            return None

        try:
            refetched = await self.get_order(order.identifier, pair)
            if refetched is not None:
                return refetched
        except DataFetchError as e:
            self.logger.warning(
                f"Could not refetch cancelled order {order.identifier}; using placement-response values: {e!s}",
            )
        return order

    async def _retry_cancel_order(
        self,
        order_id: str,
        pair: str,
    ) -> bool:
        for cancel_attempt in range(self.max_retries):
            try:
                cancel_result = await self.exchange_service.cancel_order(order_id, pair)

                if cancel_result["status"] in ("canceled", "closed"):
                    self.logger.info(f"Successfully canceled order {order_id} (status={cancel_result['status']}).")
                    return True

                self.logger.warning(f"Cancel attempt {cancel_attempt + 1} for order {order_id} failed.")

            except OrderNotFoundError:
                self.logger.info(f"Order {order_id} already gone from the exchange; treating as cancelled.")
                return True

            except OrderCancellationError as e:
                self.logger.warning(f"Error during cancel attempt {cancel_attempt + 1} for order {order_id}: {e!s}")

            await asyncio.sleep(self.retry_delay)
        return False
