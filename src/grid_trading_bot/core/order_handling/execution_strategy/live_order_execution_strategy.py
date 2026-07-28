import asyncio
import logging

from grid_trading_bot.core.services.exceptions import DataFetchError, OrderCancellationError
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface

from ..exceptions import OrderExecutionFailedError
from ..order import Order, OrderSide, OrderStatus, OrderType
from .order_execution_strategy_interface import OrderExecutionStrategyInterface


class LiveOrderExecutionStrategy(OrderExecutionStrategyInterface):
    def __init__(
        self,
        exchange_service: ExchangeInterface,
        max_retries: int = 3,
        retry_delay: int = 1,
        max_slippage: float = 0.01,
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
        price: float,
    ) -> Order | None:
        remaining_quantity = quantity
        filled_total = 0.0
        cost_total = 0.0
        last_order: Order | None = None

        for attempt in range(self.max_retries):
            try:
                raw_order = await self.exchange_service.place_order(
                    pair,
                    OrderType.MARKET.value.lower(),
                    order_side.name.lower(),
                    remaining_quantity,
                    price,
                )
                order_result = await self._parse_order_result(raw_order)
                last_order = order_result
                leg_filled = order_result.filled or 0.0
                if order_result.average is not None:
                    leg_price = order_result.average
                elif order_result.price is not None:
                    leg_price = order_result.price
                else:
                    leg_price = price

                if order_result.status == OrderStatus.CLOSED:
                    filled_total += leg_filled
                    cost_total += leg_filled * leg_price
                    return self._aggregate_market_order(order_result, quantity, filled_total, cost_total)

                elif order_result.status == OrderStatus.OPEN:
                    final_leg = await self._handle_partial_fill(order_result, pair)
                    if final_leg is None:
                        # Cannot cancel — account only the placement-response fill to avoid double-spend
                        filled_total += leg_filled
                        cost_total += leg_filled * leg_price
                        return self._aggregate_market_order(order_result, quantity, filled_total, cost_total)

                    final_leg_filled = final_leg.filled or 0.0
                    if final_leg.average is not None:
                        final_leg_price = final_leg.average
                    elif final_leg.price is not None:
                        final_leg_price = final_leg.price
                    else:
                        final_leg_price = price
                    filled_total += final_leg_filled
                    cost_total += final_leg_filled * final_leg_price
                    remaining_quantity -= final_leg_filled

                else:
                    if leg_filled > 0:
                        filled_total += leg_filled
                        cost_total += leg_filled * leg_price
                        remaining_quantity -= leg_filled
                        self.logger.warning(
                            f"Market order leg returned status {order_result.status} with partial fill "
                            f"{leg_filled}; retrying remaining {remaining_quantity}.",
                        )
                    else:
                        self.logger.warning(
                            f"Market order leg returned status {order_result.status}; retrying full remaining.",
                        )

                await asyncio.sleep(self.retry_delay)
                self.logger.info(f"Retrying order. Attempt {attempt + 1}/{self.max_retries}.")
                price = await self._adjust_price(order_side, price, attempt + 1)

            except DataFetchError as e:
                self.logger.error(f"Attempt {attempt + 1} failed with error: {e!s}")
                await asyncio.sleep(self.retry_delay)

        if filled_total > 0 and last_order is not None:
            self.logger.error(
                f"Market order exhausted retries with partial fill {filled_total}/{quantity}. "
                f"Returning aggregate so fills stay accounted.",
            )
            return self._aggregate_market_order(last_order, quantity, filled_total, cost_total)

        raise OrderExecutionFailedError(
            "Failed to execute Market order after maximum retries.",
            order_side,
            OrderType.MARKET,
            pair,
            quantity,
            price,
        )

    def _aggregate_market_order(
        self,
        last_order: Order,
        requested_quantity: float,
        filled_total: float,
        cost_total: float,
    ) -> Order:
        """Synthesizes one Order covering every executed leg of a retried market order."""
        average = cost_total / filled_total if filled_total > 0 else last_order.average
        return Order(
            identifier=last_order.identifier,
            status=OrderStatus.CLOSED,
            order_type=OrderType.MARKET,
            side=last_order.side,
            price=last_order.price,
            average=average,
            amount=requested_quantity,
            filled=filled_total,
            remaining=max(requested_quantity - filled_total, 0.0),
            timestamp=last_order.timestamp,
            datetime=last_order.datetime,
            last_trade_timestamp=last_order.last_trade_timestamp,
            symbol=last_order.symbol,
            time_in_force=last_order.time_in_force,
            trades=last_order.trades,
            fee=last_order.fee,
            cost=cost_total,
            info=last_order.info,
        )

    async def execute_limit_order(
        self,
        order_side: OrderSide,
        pair: str,
        quantity: float,
        price: float,
    ) -> Order | None:
        try:
            raw_order = await self.exchange_service.place_order(
                pair,
                OrderType.LIMIT.value.lower(),
                order_side.name.lower(),
                quantity,
                price,
            )
            order_result = await self._parse_order_result(raw_order)
            return order_result

        except DataFetchError as e:
            self.logger.error(f"DataFetchError during order execution for {pair} - {e}")
            raise OrderExecutionFailedError(
                f"Failed to execute Limit order on {pair}: {e}",
                order_side,
                OrderType.LIMIT,
                pair,
                quantity,
                price,
            ) from e

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

    async def _adjust_price(
        self,
        order_side: OrderSide,
        price: float,
        attempt: int,
    ) -> float:
        adjustment = self.max_slippage / self.max_retries * attempt
        return price * (1 + adjustment) if order_side == OrderSide.BUY else price * (1 - adjustment)

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
        self.logger.info(f"Order partially filled with {order.filled}. Attempting to cancel and retry remaining.")

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

            except OrderCancellationError as e:
                # live_exchange_service.cancel_order wraps ccxt's OrderNotFound in an
                # OrderCancellationError whose message says "not found for cancellation" —
                # that means the order is already gone from the exchange, which is the
                # outcome we wanted, so treat it as a successful cancellation.
                if "not found for cancellation" in str(e):
                    self.logger.info(f"Order {order_id} already gone from the exchange; treating as cancelled.")
                    return True
                self.logger.warning(f"Error during cancel attempt {cancel_attempt + 1} for order {order_id}: {e!s}")

            await asyncio.sleep(self.retry_delay)
        return False
