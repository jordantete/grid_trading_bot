import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
import logging
import time
from typing import Any

from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.exceptions import BookingError, OrderRefusedError
from grid_trading_bot.core.services.exceptions import DataFetchError
from grid_trading_bot.core.services.market_constraints import MarketConstraints

from .balance_tracker import BalanceTracker
from .execution_strategy.order_execution_strategy_interface import OrderExecutionStrategyInterface
from .market_entry import MarketEntrySender
from .order import Order, OrderSide
from .order_book import OrderBook
from .placement_journal import PlacementJournal, PlacementPurpose


class Liquidation:
    """Closes the positions: cancels the grid orders once, then sells the crypto at market when asked."""

    LIQUIDATION_BACKOFF = (1, 2, 4, 8, 16, 30)
    LIQUIDATION_ALERT_INTERVAL = 600.0

    def __init__(
        self,
        balance_tracker: BalanceTracker,
        order_book: OrderBook,
        journal: PlacementJournal,
        order_execution_strategy: OrderExecutionStrategyInterface,
        market_entries: MarketEntrySender,
        notification_handler: NotificationHandler,
        trading_pair: str,
        fail_fast: bool,
        notify: Callable[..., None],
        send_now: Callable[[list[tuple[NotificationType, dict[str, Any]]]], Awaitable[None]],
        locked: Callable[[], AbstractAsyncContextManager[None]],
        save_state: Callable[[], Awaitable[None]],
        cancel_grid_orders: Callable[[], Awaitable[bool]],
        market_constraints: Callable[[], MarketConstraints | None],
        exchange_balance: Callable[[], Callable[[], Awaitable[float]] | None],
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.balance_tracker = balance_tracker
        self.order_book = order_book
        self.journal = journal
        self.order_execution_strategy = order_execution_strategy
        self.market_entries = market_entries
        self.notification_handler = notification_handler
        self.trading_pair = trading_pair
        self.fail_fast = fail_fast
        self._notify = notify
        self._send_now = send_now
        self._locked = locked
        self._save_state = save_state
        self._cancel_grid_orders = cancel_grid_orders
        self._market_constraints = market_constraints
        self._exchange_balance = exchange_balance
        self.positions_closed = False
        self.liquidated = False
        self._interrupted = asyncio.Event()
        self.clock: Callable[[], float] = time.monotonic
        self.sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    async def close_positions(self, liquidate: bool, current_price: float | None = None) -> None:
        """
        Cancels every open grid order and drops the journal's limit entries, once; then, when asked, sells the crypto
        until it is sold, under the exchange minimum, or interrupted. A liquidation that failed or was interrupted
        runs again on the next call that asks for it. A liquidation without the current price is refused before
        anything is cancelled.

        Note: `cancel_grid_orders` acquires the order lock itself, so it must be awaited outside any lock held by
        this method to avoid deadlocking.
        """
        if liquidate and current_price is None:
            raise ValueError("A liquidation needs the current price.")
        if not self.positions_closed:
            self.positions_closed = True
            cancelled = await self._cancel_grid_orders()
            if not cancelled:
                self.logger.error("Could not cancel all open grid orders during shutdown.")
                await self.notification_handler.async_send_notification(
                    NotificationType.ERROR_OCCURRED,
                    error_details="Shutdown: some open grid orders could not be cancelled on the exchange.",
                )
        elif not liquidate:
            self.logger.info("close_positions already executed; skipping.")
            return
        # The first check already refused a liquidation without a price; this one narrows the type for _liquidate.
        if not liquidate or current_price is None:
            return
        if self.liquidated:
            self.logger.info("Positions already liquidated; skipping.")
            return
        await self._liquidate(current_price)

    def interrupt(self) -> None:
        """Ends a liquidation's retries; the attempt in progress, or a first one, still runs."""
        self._interrupted.set()

    @property
    def _base_currency(self) -> str:
        return self.trading_pair.split("/")[0]

    async def _liquidate(self, current_price: float) -> None:
        """
        Sells the free crypto at market until none is left. A failed attempt is retried after a growing delay,
        except a sale the exchange executed and the bot could not book, which ends the liquidation with an alert and
        keeps its entry so it is never sent again; a refusal re-reads the exchange balance first, and a remainder under
        the exchange minimum stops with an alert. Alerts at the first failure, then every LIQUIDATION_ALERT_INTERVAL
        seconds. A backtest re-raises what is neither an exchange failure nor a refusal, since nothing would ever
        interrupt its retries.
        """
        failures = 0
        last_alert_at: float | None = None
        exchange_cap: float | None = None
        while True:
            if failures and self._interrupted.is_set():
                await self._send_liquidation_alert(
                    f"Liquidation interrupted by the shutdown, {self.balance_tracker.crypto_balance} "
                    f"{self._base_currency} still open."
                )
                return
            error: Exception | None = None
            booking_error: BookingError | None = None
            sold: float | None = None
            async with self._locked():
                try:
                    sold = await self._liquidation_attempt(current_price, exchange_cap)
                except BookingError as e:
                    if self.fail_fast:
                        raise
                    booking_error = e
                except (DataFetchError, OrderRefusedError) as e:
                    error = e
                except Exception as e:
                    error = e
                    self.logger.error(f"Liquidation attempt {failures + 1} raised: {e}", exc_info=True)
                    if self.fail_fast:
                        raise
                finally:
                    await self._save_state()
            if booking_error is not None:
                await self._send_liquidation_alert(
                    f"Liquidation sale not booked: {booking_error}. The bot halts. Check this order on the exchange, "
                    "then see going-live.md for resume vs keeping the database.",
                    exc_info=booking_error,
                )
                return
            if error is None:
                if sold is None:
                    self.liquidated = True
                    return
                if exchange_cap is not None:
                    exchange_cap = max(exchange_cap - sold, 0.0)
                continue
            failures += 1
            backoff = self.LIQUIDATION_BACKOFF
            delay = backoff[min(failures, len(backoff)) - 1]
            self.logger.warning(f"Liquidation attempt {failures} failed: {error}; next attempt in {delay} s.")
            if isinstance(error, OrderRefusedError):
                exchange_cap = await self._read_exchange_free_base(exchange_cap)
            now = self.clock()
            if last_alert_at is None or now - last_alert_at >= self.LIQUIDATION_ALERT_INTERVAL:
                last_alert_at = now
                await self._send_liquidation_alert(self._liquidation_failure_text(failures, error))
            if not self._interrupted.is_set():
                await self._pause(delay)

    async def _liquidation_attempt(self, current_price: float, exchange_cap: float | None) -> float | None:
        """
        One market sell, under the lock: a leg whose outcome is unknown is looked up first, otherwise the leg a failed
        attempt left, or a new one, sells the free crypto. Returns the quantity sold, or None once nothing more can be
        sold. Raises DataFetchError while the outcome is unknown or the order was not placed, OrderRefusedError on a
        refusal.
        """
        entry = next((e for e in self.journal.entries() if e.purpose is PlacementPurpose.LIQUIDATION), None)
        if entry is None or not entry.sent:
            quantity = self._liquidation_quantity(current_price, exchange_cap)
            if quantity is None:
                if entry is not None:
                    self.journal.remove(entry.client_order_id)
                return None
            if entry is None:
                # The entry stays in the journal until the liquidation ends, so a restart in between halts.
                entry = self.market_entries.new_entry(
                    OrderSide.SELL, quantity, current_price, PlacementPurpose.LIQUIDATION
                )
            else:
                entry.quantity = quantity
        order = await self.market_entries.send(entry)
        try:
            sold = await self._book_liquidation(order)
        except Exception as e:
            raise BookingError(order, e) from e
        self.journal.remove(entry.client_order_id)
        return sold

    def _liquidation_quantity(self, current_price: float, exchange_cap: float | None) -> float | None:
        """The quantity of the next leg, or None when nothing can be sold."""
        quantity = self.balance_tracker.crypto_balance
        if exchange_cap is not None:
            quantity = min(quantity, exchange_cap)
        if quantity > 0:
            try:
                quantity = self.order_execution_strategy.round_amount(self.trading_pair, quantity)
            except OrderRefusedError as e:
                self._stop_liquidation(
                    f"Liquidation stopped: {quantity} {self._base_currency} left, which the exchange cannot round "
                    f"({e})."
                )
                return None
        if quantity <= 0:
            if self.balance_tracker.crypto_balance > 0:
                self._stop_liquidation(
                    f"Liquidation stopped: the exchange holds no more free {self._base_currency}; the books still "
                    f"show {self.balance_tracker.crypto_balance} {self._base_currency}."
                )
            else:
                self.logger.info("No crypto balance left to liquidate.")
            return None
        market_constraints = self._market_constraints()
        if market_constraints is not None:
            violation = market_constraints.violation(quantity, current_price)
            if violation:
                self._stop_liquidation(
                    f"Liquidation stopped: {quantity} {self._base_currency} left, under the exchange minimum "
                    f"({violation})."
                )
                return None
        return quantity

    def _stop_liquidation(self, details: str) -> None:
        self.logger.error(details)
        self._notify(NotificationType.ERROR_OCCURRED, error_details=details)

    async def _book_liquidation(self, order: Order) -> float:
        await self.balance_tracker.update_after_liquidation(order)
        self.order_book.add_order(order)
        return order.filled

    async def _read_exchange_free_base(self, fallback: float | None) -> float | None:
        exchange_balance = self._exchange_balance()
        if exchange_balance is None:
            return fallback
        try:
            return await exchange_balance()
        except DataFetchError as e:
            self.logger.warning(f"Could not read the exchange balance for the next liquidation attempt: {e}")
        except Exception as e:
            self.logger.error(f"Reading the exchange balance for the next attempt failed: {e}", exc_info=True)
            if self.fail_fast:
                raise
        return fallback

    async def _pause(self, delay: float) -> None:
        """Waits `delay` seconds, or until the liquidation is interrupted."""
        sleeper = asyncio.ensure_future(self.sleep(delay))
        interrupted = asyncio.ensure_future(self._interrupted.wait())
        try:
            await asyncio.wait({sleeper, interrupted}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            interrupted.cancel()

    def _liquidation_failure_text(self, failures: int, error: Exception) -> str:
        position = f"{self.balance_tracker.crypto_balance} {self._base_currency}"
        if failures == 1:
            return f"Liquidation of {position} failed ({error}); retrying until it succeeds."
        return f"Liquidation still failing after {failures} attempts ({error}); {position} still open."

    async def _send_liquidation_alert(self, details: str, exc_info: BaseException | None = None) -> None:
        self.logger.error(details, exc_info=exc_info)
        await self._send_now([(NotificationType.ERROR_OCCURRED, {"error_details": details})])

    async def execute_take_profit_or_stop_loss_order(
        self,
        current_price: float,
        take_profit_order: bool = False,
        stop_loss_order: bool = False,
    ) -> None:
        """
        Closes the position after a take-profit or stop-loss event: `close_positions` cancels
        every open grid order, then liquidates the full crypto balance, in every trading mode.

        Args:
            current_price (float): The current market price triggering the event.
            take_profit_order (bool): Indicates whether this is a take-profit event.
            stop_loss_order (bool): Indicates whether this is a stop-loss event.
        """
        if not (take_profit_order or stop_loss_order):
            self.logger.warning("No take profit or stop loss action specified.")
            return

        event = "Take profit" if take_profit_order else "Stop loss"
        await self.close_positions(liquidate=True, current_price=current_price)
        left = self.balance_tracker.crypto_balance
        if not self.liquidated:
            outcome = "liquidation incomplete (see the liquidation alerts)"
        elif left > 0:
            outcome = f"liquidation stopped with {left} {self._base_currency} left (see the liquidation alerts)"
        else:
            outcome = "position liquidated"
        await self.notification_handler.async_send_notification(
            NotificationType.TAKE_PROFIT_TRIGGERED if take_profit_order else NotificationType.STOP_LOSS_TRIGGERED,
            order_details=f"{event} at {current_price}: open orders cancelled, {outcome}.",
        )
        self.logger.info(f"{event} triggered at {current_price}; close_positions executed.")
