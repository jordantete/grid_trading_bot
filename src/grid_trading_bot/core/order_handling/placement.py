from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
import copy
import logging
import time

from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.exceptions import OrderRefusedError
from grid_trading_bot.core.services.exceptions import DataFetchError, OrderNotPlacedError, PostOnlyRejectedError
from grid_trading_bot.core.services.market_constraints import MarketConstraints

from ..grid_management.grid_level import GridLevel
from ..grid_management.grid_manager import GridManager
from ..validation.exceptions import InvalidOrderQuantityError
from ..validation.order_validator import OrderValidator
from .balance_tracker import BalanceTracker
from .execution_strategy.order_execution_strategy_interface import OrderExecutionStrategyInterface
from .order import Order, OrderSide, OrderStatus, OrderType
from .order_book import OrderBook
from .placement_journal import PendingPlacement, PlacementJournal, PlacementPurpose, adopt_order, new_client_order_id


class Placement:
    """Places limit orders through the journal, retries the entries it holds and reports what it could not place."""

    def __init__(
        self,
        grid_manager: GridManager,
        order_validator: OrderValidator,
        balance_tracker: BalanceTracker,
        order_book: OrderBook,
        order_execution_strategy: OrderExecutionStrategyInterface,
        journal: PlacementJournal,
        trading_pair: str,
        config_hash: str,
        notify: Callable[..., None],
        locked: Callable[[], AbstractAsyncContextManager[None]],
        save_state: Callable[[], Awaitable[None]],
        save_pending: Callable[[PendingPlacement], Awaitable[bool]],
        market_constraints: Callable[[], MarketConstraints | None],
        positions_closed: Callable[[], bool],
        apply_update: Callable[[Order, Order], Awaitable[None]],
        report_unfunded: Callable[[list[tuple[OrderSide, float]]], None],
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.grid_manager = grid_manager
        self.order_validator = order_validator
        self.balance_tracker = balance_tracker
        self.order_book = order_book
        self.order_execution_strategy = order_execution_strategy
        self.journal = journal
        self.trading_pair = trading_pair
        self.config_hash = config_hash
        self._notify = notify
        self._locked = locked
        self._save_state = save_state
        self._save_pending = save_pending
        self._market_constraints = market_constraints
        self._positions_closed = positions_closed
        self._apply_update = apply_update
        self._report_unfunded = report_unfunded
        self._retry_errors_alerted: set[str] = set()

    async def submit_limit(
        self,
        side: OrderSide,
        grid_level: GridLevel,
        quantity: float,
        purpose: PlacementPurpose,
        source_level: GridLevel | None = None,
        source_order_id: str | None = None,
    ) -> Order | None:
        """
        Places one limit order on `grid_level` through the journal: rounds and checks it, reserves its funds, holds
        the level, saves the entry, then sends it. Returns the order once the exchange holds it and None while the
        entry stays pending; raises OrderRefusedError when the bot or the exchange refuses it.
        """
        if self._positions_closed():
            self.logger.info(f"Positions are closed; not placing {side.value} order at {grid_level.price}.")
            return None
        price = self.order_execution_strategy.round_price(self.trading_pair, grid_level.price)
        validated = self._validate_order_quantity(side, quantity, price)
        rounded = self.order_execution_strategy.round_amount(self.trading_pair, validated)
        if rounded <= 0:
            raise InvalidOrderQuantityError(f"{side.value.capitalize()} quantity {validated} rounds to {rounded}.")
        market_constraints = self._market_constraints()
        if market_constraints is not None:
            violation = market_constraints.violation(rounded, price)
            if violation:
                raise InvalidOrderQuantityError(f"Order at {price}: {violation}")
        await self._reserve_funds(side, rounded, price)
        entry = PendingPlacement(
            client_order_id=new_client_order_id(self.config_hash),
            order_type=OrderType.LIMIT,
            side=side,
            quantity=rounded,
            price=price,
            purpose=purpose,
            level_price=grid_level.price,
            source_level_price=source_level.price if source_level is not None else None,
            source_order_id=source_order_id,
            created_at=int(time.time() * 1000),
        )
        self.journal.add(entry)
        self.grid_manager.mark_level_pending(grid_level, side)
        return await self._send_entry(entry)

    async def _send_entry(self, entry: PendingPlacement) -> Order | None:
        """
        Saves `entry` as sent, then sends it. Returns the order the exchange holds, or None while the entry waits for
        a retry; a refusal drops the entry and raises OrderRefusedError.
        """
        entry.sent = True
        if not await self._save_pending(entry):
            entry.sent = False
            self.logger.warning(f"Placement {entry.client_order_id} could not be saved; it is sent at the next poll.")
            return None
        entry.attempts += 1
        try:
            order = await self.order_execution_strategy.execute_limit_order(
                entry.side, self.trading_pair, entry.quantity, entry.limit_price, entry.client_order_id
            )
        except PostOnlyRejectedError as e:
            entry.sent = False
            self.logger.info(
                f"Maker-only {entry.side.value} order at {entry.price} would have crossed ({e}); "
                "retried at the next poll."
            )
            return None
        except OrderRefusedError:
            await self.drop_entry(entry)
            raise
        except OrderNotPlacedError as e:
            entry.sent = False
            self._report_pending(entry, e)
            return None
        except DataFetchError as e:
            # The request may have reached the exchange, so the next retry looks the order up before sending again.
            self._report_pending(entry, e)
            return None
        except Exception as e:
            await self.drop_entry(entry)
            context = f"Placement of the {entry.side.value} order at {entry.price} failed"
            self.logger.error(f"{context}: {e}", exc_info=True)
            self._notify(NotificationType.ERROR_OCCURRED, error_details=f"{context}: {e}")
            return None
        return self._adopt(entry, order)

    def _adopt(self, entry: PendingPlacement, order: Order) -> Order:
        """Moves an order the exchange holds for `entry` from the journal into the book."""
        adopt_order(entry, order, self.grid_manager, self.order_book)
        self.journal.remove(entry.client_order_id)
        self._retry_errors_alerted.discard(entry.client_order_id)
        if entry.attempts > 1:
            self.logger.info(f"Placement {entry.client_order_id} placed after {entry.attempts} attempts.")
        if entry.purpose is PlacementPurpose.PAIRED:
            self._notify(NotificationType.ORDER_PLACED, order_details=str(order))
        return order

    async def drop_entry(self, entry: PendingPlacement) -> None:
        """Removes a limit entry that will not become an order: releases its funds and frees its level."""
        self.journal.remove(entry.client_order_id)
        self._retry_errors_alerted.discard(entry.client_order_id)
        await self._release_funds(entry.side, entry.quantity, entry.limit_price)
        grid_level = self.grid_manager.grid_levels.get(entry.level_price) if entry.level_price is not None else None
        if grid_level is not None:
            self.grid_manager.rollback_order_placement(grid_level, entry.side, self.level_busy(grid_level))

    def level_busy(self, grid_level: GridLevel) -> bool:
        """Whether an open order or a journal entry still holds `grid_level`."""
        return self.order_book.has_open_orders_at(grid_level) or bool(self.journal.pending_at(grid_level.price))

    def _report_pending(self, entry: PendingPlacement, error: Exception) -> None:
        self.logger.warning(
            f"{entry.side.value.capitalize()} order at {entry.price} not placed yet ({error}); "
            "retried at the next poll."
        )
        if entry.attempts == 1:
            self._notify(
                NotificationType.ORDER_PENDING,
                error_details=f"{entry.side.value} {entry.quantity} at {entry.price}: {error}",
            )

    def report_refusal(self, purpose: PlacementPurpose, price: float, error: OrderRefusedError) -> None:
        """Reports a refused placement; a paired or re-placed order the grid expected also raises an alert."""
        details = f"{purpose.value.capitalize()} order at {price} refused: {error}"
        self.logger.warning(details)
        if purpose in (PlacementPurpose.PAIRED, PlacementPurpose.REPLACEMENT):
            self._notify(NotificationType.ORDER_FAILED, error_details=details)

    async def retry_pending_placements(self) -> None:
        """Resolves or sends again, in creation order, every limit placement the journal holds, then saves."""
        async with self._locked():
            if not self.journal.limit_entries():
                return
            try:
                if self._positions_closed():
                    # A sent entry may be an order the exchange holds, so only a lookup may drop it.
                    for entry in [entry for entry in self.journal.limit_entries() if not entry.sent]:
                        await self.drop_entry(entry)
                    return
                for entry in self.journal.limit_entries():
                    if self.journal.get(entry.client_order_id) is entry:
                        await self._retry_entry(entry)
            finally:
                await self._save_state()

    async def _retry_entry(self, entry: PendingPlacement) -> None:
        level_price = entry.level_price
        if level_price is None:
            self._report_retry_error(entry, RuntimeError(f"limit placement {entry.client_order_id} has no level price"))
            return
        try:
            if entry.sent and await self.resolve_sent_entry(entry) is not None:
                return
            await self._send_entry(entry)
        except OrderRefusedError as e:
            if entry.purpose is PlacementPurpose.GRID:
                self._report_unfunded([(entry.side, level_price)])
            else:
                self.report_refusal(entry.purpose, level_price, e)
        except DataFetchError as e:
            self.logger.warning(f"Could not look up placement {entry.client_order_id}; retried at the next poll: {e}")
        except Exception as e:
            self._report_retry_error(entry, e)

    def _report_retry_error(self, entry: PendingPlacement, error: Exception) -> None:
        """Logs a retry that failed unexpectedly, and alerts once per entry."""
        details = f"Retry of placement {entry.client_order_id} failed: {error}"
        self.logger.error(details, exc_info=error)
        if entry.client_order_id not in self._retry_errors_alerted:
            self._retry_errors_alerted.add(entry.client_order_id)
            self._notify(NotificationType.ERROR_OCCURRED, error_details=details)

    async def resolve_sent_entry(self, entry: PendingPlacement) -> Order | None:
        """
        Looks a sent entry up by its client id. Adopts the order the exchange holds and applies what it already did, or
        marks the entry unsent when the exchange never received it. Raises DataFetchError when the lookup fails.
        """
        found = await self.order_execution_strategy.find_order(entry.client_order_id, self.trading_pair)
        if found is None:
            entry.sent = False
            return None
        local = self._adopt(entry, copy.copy(found))
        if found.status != OrderStatus.OPEN:
            await self._apply_update(local, found)
        return local

    async def replace_on_level(self, order: Order, grid_level: GridLevel) -> None:
        """Re-places an order that ended unfilled on its level, for the quantity it did not fill."""
        quantity = order.remaining if order.remaining and order.remaining > 0 else order.amount
        try:
            await self.submit_limit(
                order.side, grid_level, quantity, PlacementPurpose.REPLACEMENT, source_order_id=order.identifier
            )
        except OrderRefusedError as e:
            self.report_refusal(PlacementPurpose.REPLACEMENT, grid_level.price, e)
        except Exception as e:
            details = f"Failed to re-place order {order.identifier} at grid level {grid_level.price}: {e}"
            self.logger.error(details, exc_info=True)
            self._notify(NotificationType.ERROR_OCCURRED, error_details=details)

    def _validate_order_quantity(self, side: OrderSide, order_quantity: float, price: float) -> float:
        if side == OrderSide.BUY:
            return self.order_validator.adjust_and_validate_buy_quantity(
                balance=self.balance_tracker.balance,
                order_quantity=order_quantity,
                price=price,
            )
        return self.order_validator.adjust_and_validate_sell_quantity(
            crypto_balance=self.balance_tracker.crypto_balance,
            order_quantity=order_quantity,
        )

    async def _reserve_funds(self, side: OrderSide, quantity: float, price: float) -> None:
        if side == OrderSide.BUY:
            await self.balance_tracker.reserve_funds_for_buy(quantity * price)
        else:
            await self.balance_tracker.reserve_funds_for_sell(quantity)

    async def _release_funds(self, side: OrderSide, quantity: float, price: float) -> None:
        if side == OrderSide.BUY:
            await self.balance_tracker.release_reserved_fiat(quantity * price)
        else:
            await self.balance_tracker.release_reserved_crypto(quantity)

    def handle_order_error(self, error: Exception, context: str) -> None:
        """Logs a failed order operation and alerts, as ORDER_FAILED for an exchange failure or a refusal."""
        if isinstance(error, DataFetchError | OrderRefusedError):
            self.logger.error(f"{context} - {error!s}", exc_info=True)
            self._notify(
                NotificationType.ORDER_FAILED,
                error_details=f"{context}. {error}",
            )
        else:
            self.logger.error(f"Unexpected error: {context}: {error}", exc_info=True)
            self._notify(
                NotificationType.ERROR_OCCURRED,
                error_details=f"{context}: {error!s}",
            )
