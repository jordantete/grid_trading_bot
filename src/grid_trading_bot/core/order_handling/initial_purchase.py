from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
import logging

from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.exceptions import BookingError, OrderRefusedError
from grid_trading_bot.core.services.exceptions import DataFetchError
from grid_trading_bot.core.services.market_constraints import MarketConstraints

from ..grid_management.grid_manager import GridManager
from .balance_tracker import BalanceTracker
from .market_entry import MarketEntrySender
from .order import Order, OrderSide, OrderStatus
from .order_book import OrderBook
from .placement_journal import PlacementJournal, PlacementPurpose


class InitialPurchase:
    """Buys at market, once and in at most MAX_MARKET_LEGS legs, the crypto the grid's sells need."""

    MAX_MARKET_LEGS = 3

    def __init__(
        self,
        grid_manager: GridManager,
        balance_tracker: BalanceTracker,
        order_book: OrderBook,
        journal: PlacementJournal,
        market_entries: MarketEntrySender,
        notify: Callable[..., None],
        locked: Callable[[], AbstractAsyncContextManager[None]],
        save_state: Callable[[], Awaitable[None]],
        market_constraints: Callable[[], MarketConstraints | None],
        positions_closed: Callable[[], bool],
        purchase_done: Callable[[], bool],
        mark_purchase_done: Callable[[], None],
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.grid_manager = grid_manager
        self.balance_tracker = balance_tracker
        self.order_book = order_book
        self.journal = journal
        self.market_entries = market_entries
        self._notify = notify
        self._locked = locked
        self._save_state = save_state
        self._market_constraints = market_constraints
        self._positions_closed = positions_closed
        self._purchase_done = purchase_done
        self._mark_purchase_done = mark_purchase_done

    async def perform(self, current_price: float) -> bool:
        """
        Buys, once, the crypto the grid's sells need. Returns whether the purchase is done; it is not while the
        exchange cannot take or confirm the order, and the caller tries again at its next tick.
        """
        async with self._locked():
            if self._purchase_done():
                self.logger.info("Initial purchase already done; skipping it.")
                return True
            done = False
            try:
                if self._positions_closed():
                    self.logger.info("Positions are closed; skipping the initial purchase.")
                    done = True
                else:
                    done = await self._buy_initial_position(current_price)
                return done
            finally:
                if done:
                    self._mark_purchase_done()
                await self._save_state()

    async def _buy_initial_position(self, current_price: float) -> bool:
        entry = next((e for e in self.journal.entries() if e.purpose is PlacementPurpose.INITIAL_PURCHASE), None)
        if entry is None:
            quantity = self.grid_manager.get_initial_order_quantity(
                current_fiat_balance=self.balance_tracker.balance,
                current_crypto_balance=self.balance_tracker.crypto_balance,
                current_price=current_price,
            )
            if quantity <= 0:
                self.logger.warning("Initial purchase quantity is zero or negative. Skipping initial purchase.")
                return True
            market_constraints = self._market_constraints()
            if market_constraints is not None:
                violation = market_constraints.violation(quantity, current_price)
                if violation:
                    self.logger.warning(f"Skipping initial purchase: {violation}")
                    self._notify(NotificationType.ORDER_FAILED, error_details=f"Initial purchase skipped: {violation}")
                    return True
            try:
                entry = self.market_entries.new_entry(
                    OrderSide.BUY, quantity, current_price, PlacementPurpose.INITIAL_PURCHASE
                )
            except OrderRefusedError as e:
                self.logger.warning(f"Skipping initial purchase: {e}")
                self._notify(NotificationType.ORDER_FAILED, error_details=f"Initial purchase skipped: {e}")
                return True

        for _ in range(self.MAX_MARKET_LEGS):
            self.logger.info(f"Performing initial crypto purchase: {entry.quantity} at price {current_price}.")
            try:
                order = await self.market_entries.send(entry)
            except OrderRefusedError as e:
                self.journal.remove(entry.client_order_id)
                self.logger.warning(f"Initial purchase refused: {e}")
                self._notify(
                    NotificationType.ORDER_FAILED, error_details=f"Error while performing initial purchase. {e}"
                )
                return True
            except DataFetchError as e:
                self.logger.warning(f"Initial purchase not done yet; retried at the next tick: {e}")
                if entry.attempts == 1:
                    self._notify(
                        NotificationType.ORDER_PENDING, error_details=f"Initial purchase of {entry.quantity}: {e}"
                    )
                return False
            except Exception as e:
                self.logger.error(f"Unexpected error during the initial purchase: {e}", exc_info=True)
                if entry.attempts == 1:
                    self._notify(
                        NotificationType.ERROR_OCCURRED, error_details=f"Error while performing initial purchase: {e}"
                    )
                return False
            try:
                await self._book_initial_purchase(order)
            except Exception as e:
                error = BookingError(order, e)
                self.logger.error(f"Initial purchase not booked: {error}", exc_info=True)
                self._notify(
                    NotificationType.ERROR_OCCURRED,
                    error_details=(
                        f"Initial purchase not booked: {error}. The bot halts. Check this order on the exchange, "
                        "then see going-live.md for resume vs keeping the database."
                    ),
                )
                raise error from e
            self.journal.remove(entry.client_order_id)
            remaining = entry.quantity - order.filled
            if self._is_dust(remaining, current_price):
                return True
            try:
                entry = self.market_entries.new_entry(
                    OrderSide.BUY, remaining, current_price, PlacementPurpose.INITIAL_PURCHASE
                )
            except OrderRefusedError as e:
                self.logger.warning(f"Initial purchase remainder {remaining} is dust for the exchange: {e}")
                return True

        self.journal.remove(entry.client_order_id)
        self.logger.warning(
            f"Initial purchase stopped after {self.MAX_MARKET_LEGS} legs; {entry.quantity} left unbought."
        )
        return True

    async def _book_initial_purchase(self, order: Order) -> None:
        # A leg whose remainder was cancelled is final for what it filled; the next leg buys the rest.
        order.status = OrderStatus.CLOSED
        await self.balance_tracker.update_after_initial_purchase(initial_order=order)
        self.logger.info(f"Initial crypto purchase completed. Order details: {order}")
        self.order_book.add_order(order)
        self._notify(NotificationType.ORDER_PLACED, order_details=f"Initial purchase done: {order!s}")

    def _is_dust(self, quantity: float, price: float) -> bool:
        if quantity <= 0:
            return True
        market_constraints = self._market_constraints()
        return market_constraints is not None and market_constraints.violation(quantity, price) is not None
