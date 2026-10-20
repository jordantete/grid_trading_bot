import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
import copy
from dataclasses import replace
import logging
import time
from typing import Any

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.exceptions import OrderRefusedError
from grid_trading_bot.core.services.exceptions import (
    DataFetchError,
    OrderNotPlacedError,
    OrderOutcomeUnknownError,
    PostOnlyRejectedError,
)
from grid_trading_bot.core.services.market_constraints import MarketConstraints

from ..grid_management.grid_level import GridLevel
from ..grid_management.grid_manager import GridManager
from ..order_handling.balance_tracker import BalanceTracker
from ..order_handling.order_book import OrderBook
from ..validation.exceptions import InvalidOrderQuantityError
from ..validation.order_validator import OrderValidator
from .exceptions import GridFeasibilityError, OrderExecutionFailedError
from .execution_strategy.order_execution_strategy_interface import (
    OrderExecutionStrategyInterface,
)
from .order import ENDED_UNFILLED_STATUSES, Order, OrderSide, OrderStatus, OrderType
from .placement_journal import PendingPlacement, PlacementJournal, PlacementPurpose, adopt_order, new_client_order_id
from .state_store import ArmingFlags, StateStore


class OrderManager:
    MAX_MARKET_LEGS = 3

    def __init__(
        self,
        grid_manager: GridManager,
        order_validator: OrderValidator,
        balance_tracker: BalanceTracker,
        order_book: OrderBook,
        order_execution_strategy: OrderExecutionStrategyInterface,
        notification_handler: NotificationHandler,
        trading_mode: TradingMode,
        trading_pair: str,
        state_store: StateStore | None = None,
        journal: PlacementJournal | None = None,
        config_hash: str = "",
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.grid_manager = grid_manager
        self.order_validator = order_validator
        self.balance_tracker = balance_tracker
        self.order_book = order_book
        self.order_execution_strategy = order_execution_strategy
        self.notification_handler = notification_handler
        self.trading_mode: TradingMode = trading_mode
        self.trading_pair = trading_pair
        self.state_store = state_store
        self.journal = journal if journal is not None else PlacementJournal()
        self.config_hash = config_hash
        self.arming = ArmingFlags()
        self.market_constraints: MarketConstraints | None = None
        self._lock = asyncio.Lock()
        self._outbox: list[tuple[NotificationType, dict[str, Any]]] = []
        self._positions_closed = False
        self._resolving_for_cancel = False

    def set_market_constraints(self, constraints: MarketConstraints) -> None:
        self.market_constraints = constraints

    def restore_arming(self, arming: ArmingFlags) -> None:
        self.arming = arming

    @asynccontextmanager
    async def _locked(self) -> AsyncIterator[None]:
        """Holds the order lock for one operation, then sends what it queued through `_notify`, once released."""
        queued: list[tuple[NotificationType, dict[str, Any]]] = []
        try:
            async with self._lock:
                self._outbox = queued
                yield
        finally:
            await self._send(queued)

    def _notify(self, notification_type: NotificationType, **details: Any) -> None:
        self._outbox.append((notification_type, details))

    async def _send(self, queued: list[tuple[NotificationType, dict[str, Any]]]) -> None:
        for notification_type, details in queued:
            try:
                await self.notification_handler.async_send_notification(notification_type, **details)
            except Exception as error:
                self.logger.error(f"Notification {notification_type.name} could not be sent: {error}", exc_info=True)

    async def checkpoint(self, lock_timeout: float | None = None) -> None:
        """
        Saves the state once no order operation holds the lock. Gives up after `lock_timeout` seconds: every
        completed operation already saved its own state.
        """
        if self.state_store is None:
            return
        try:
            await asyncio.wait_for(self._lock.acquire(), lock_timeout)
        except TimeoutError:
            self.logger.warning(f"An order operation still held the lock after {lock_timeout} s; checkpoint skipped.")
            return
        try:
            await self._save_state()
        finally:
            self._lock.release()

    async def _save_state(self) -> None:
        if self.state_store is not None:
            await self.state_store.save(self.arming)

    def validate_grid_feasibility(self, current_price: float) -> None:
        """
        Validates that every grid level produces an order above the exchange minimums.
        Raises GridFeasibilityError naming the offending levels; no-op without constraints.
        """
        if self.market_constraints is None:
            return
        total_balance_value = self.balance_tracker.get_total_balance_value(current_price)
        violations = []
        for side, grids in (
            (OrderSide.BUY, self.grid_manager.sorted_buy_grids),
            (OrderSide.SELL, self.grid_manager.sorted_sell_grids),
        ):
            quantity = self.grid_manager.get_order_size_for_grid_level(total_balance_value, current_price, side)
            for price in grids:
                violation = self.market_constraints.violation(quantity, price)
                if violation:
                    violations.append(f"{side.value} level {price}: {violation}")
        if violations:
            raise GridFeasibilityError(
                "Grid is not feasible on this exchange — increase capital or reduce num_grids. " + "; ".join(violations)
            )

    async def initialize_grid_orders(
        self,
        current_price: float,
    ):
        """
        Places the initial grid orders the strategy plans for the current price, then saves the state.
        """
        async with self._locked():
            try:
                await self._place_initial_orders(current_price)
                self.arming = replace(self.arming, grid_orders_initialized=True)
            finally:
                await self._save_state()

    async def _place_initial_orders(self, current_price: float) -> None:
        unfunded: list[tuple[OrderSide, float]] = []
        for grid_level, side in self.grid_manager.get_initial_order_plan(current_price):
            if self._positions_closed:
                self.logger.info("Positions are closed; skipping the remaining initial grid orders.")
                break
            if not self.grid_manager.can_place_order(grid_level, side):
                continue
            total_balance_value = self.balance_tracker.get_total_balance_value(current_price)
            quantity = self.grid_manager.get_order_size_for_grid_level(total_balance_value, current_price, side)
            try:
                await self._submit_limit(side, grid_level, quantity, PlacementPurpose.GRID)
            except OrderRefusedError as e:
                self.logger.info(f"Initial {side.value} order at {grid_level.price} not placed: {e}")
                unfunded.append((side, grid_level.price))
            except Exception as e:
                self.logger.error(
                    f"Error while placing initial {side.value} order at {grid_level.price}: {e}", exc_info=True
                )
                self._notify(
                    NotificationType.ERROR_OCCURRED,
                    error_details=f"Error while placing initial {side.value} order: {e!s}",
                )
        if unfunded:
            self._report_unfunded(unfunded)

    def _report_unfunded(self, unfunded: list[tuple[OrderSide, float]]) -> None:
        """Reports, once per grid round, the levels the balances or the exchange left without an order."""
        buys = sum(1 for side, _ in unfunded if side == OrderSide.BUY)
        prices = sorted(price for _, price in unfunded)
        details = f"Grid placement left {buys} buy and {len(unfunded) - buys} sell levels unfunded: {prices}"
        self.logger.warning(details)
        self._notify(NotificationType.ORDER_FAILED, error_details=details)

    async def cancel_open_grid_orders(self) -> bool:
        """
        Cancels all open grid orders, releases their reserved funds and resets
        their grid level states so orders can be re-placed.

        Returns True only if every open order was cancelled. On the first
        failure it stops and returns False, leaving remaining orders open.
        """
        async with self._locked():
            try:
                return await self._cancel_open_grid_orders()
            finally:
                await self._save_state()

    async def regrid(self, center_price: float, rebuild: Callable[[], None]) -> bool:
        """
        Cancels the open grid orders, rebuilds the geometry with `rebuild`, then places the orders the strategy plans
        at center_price, under one lock and with one save, so no saved state shows the grid half rebuilt. A failed
        cancellation or a rejected rebuild (ValueError) re-places orders on the current grid and returns False.
        """
        async with self._locked():
            try:
                if not await self._cancel_open_grid_orders():
                    self.logger.warning("Regrid aborted: could not cancel all open orders. Re-placing cancelled ones.")
                    await self._place_initial_orders(center_price)
                    return False
                try:
                    rebuild()
                except ValueError as e:
                    self.logger.warning(f"Regrid rejected ({e}). Re-placing orders on the existing grid.")
                    await self._place_initial_orders(center_price)
                    return False
                await self._place_initial_orders(center_price)
                return True
            finally:
                await self._save_state()

    async def _cancel_open_grid_orders(self) -> bool:
        self._resolving_for_cancel = True
        try:
            for entry in [entry for entry in self.journal.limit_entries() if entry.sent]:
                try:
                    await self._resolve_sent_entry(entry)
                except DataFetchError as e:
                    self.logger.error(
                        f"Could not look up placement {entry.client_order_id}; aborting bulk cancellation: {e}"
                    )
                    return False
        finally:
            self._resolving_for_cancel = False
        open_orders = set(self.order_book.get_open_orders())
        pairs = [
            (order, grid_level)
            for order, grid_level in (
                self.order_book.get_buy_orders_with_grid() + self.order_book.get_sell_orders_with_grid()
            )
            if order in open_orders
        ]

        for order, grid_level in pairs:
            cancelled = await self.order_execution_strategy.cancel_order(order.identifier, self.trading_pair)
            if not cancelled:
                self.logger.error(f"Failed to cancel order {order.identifier}; aborting bulk cancellation.")
                return False

            fetched = await self._refetch_cancelled(order)
            newly_filled = order.apply_update(fetched) if fetched is not None else 0.0
            if not order.is_filled():
                order.status = OrderStatus.CANCELED
            self.order_book.remove_open_order(order)
            await self.balance_tracker.record_execution(order, newly_filled)
            if grid_level is not None:
                self.grid_manager.rollback_order_placement(grid_level, order.side, self._level_busy(grid_level))
            self.logger.info(f"Cancelled {order.side.value} order {order.identifier} at {order.price}.")

        for entry in self.journal.limit_entries():
            await self._drop_entry(entry)
        return True

    async def _refetch_cancelled(self, order: Order) -> Order | None:
        """The exchange's view of an order just cancelled; None in backtest or when it cannot be read."""
        if self.trading_mode == TradingMode.BACKTEST:
            return None
        try:
            return await self.order_execution_strategy.get_order(order.identifier, self.trading_pair)
        except DataFetchError as e:
            self.logger.warning(f"Could not refetch cancelled order {order.identifier}; using local state: {e}")
            return None

    async def _submit_limit(
        self,
        side: OrderSide,
        grid_level: GridLevel,
        quantity: float,
        purpose: PlacementPurpose,
        source_level: GridLevel | None = None,
    ) -> Order | None:
        """
        Places one limit order on `grid_level` through the journal: rounds and checks it, reserves its funds, holds
        the level, saves the entry, then sends it. Returns the order once the exchange holds it and None while the
        entry stays pending; raises OrderRefusedError when the bot or the exchange refuses it.
        """
        if self._positions_closed:
            self.logger.info(f"Positions are closed; not placing {side.value} order at {grid_level.price}.")
            return None
        price = self.order_execution_strategy.round_price(self.trading_pair, grid_level.price)
        validated = self._validate_order_quantity(side, quantity, price)
        rounded = self.order_execution_strategy.round_amount(self.trading_pair, validated)
        if rounded <= 0:
            raise InvalidOrderQuantityError(f"{side.value.capitalize()} quantity {validated} rounds to {rounded}.")
        if self.market_constraints is not None:
            violation = self.market_constraints.violation(rounded, price)
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
        if self.state_store is not None and not await self.state_store.save_pending(entry):
            entry.sent = False
            self.logger.warning(f"Placement {entry.client_order_id} could not be saved; it is sent at the next poll.")
            return None
        entry.attempts += 1
        try:
            order = await self.order_execution_strategy.execute_limit_order(
                entry.side, self.trading_pair, entry.quantity, entry.price, entry.client_order_id
            )
        except PostOnlyRejectedError as e:
            entry.sent = False
            self.logger.info(
                f"Maker-only {entry.side.value} order at {entry.price} would have crossed ({e}); "
                "retried at the next poll."
            )
            return None
        except OrderRefusedError:
            await self._drop_entry(entry)
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
            await self._drop_entry(entry)
            context = f"Placement of the {entry.side.value} order at {entry.price} failed"
            self.logger.error(f"{context}: {e}", exc_info=True)
            notification = (
                NotificationType.ORDER_FAILED
                if isinstance(e, OrderExecutionFailedError)
                else NotificationType.ERROR_OCCURRED
            )
            self._notify(notification, error_details=f"{context}: {e}")
            return None
        return self._adopt(entry, order)

    def _adopt(self, entry: PendingPlacement, order: Order) -> Order:
        """Moves an order the exchange holds for `entry` from the journal into the book."""
        adopt_order(entry, order, self.grid_manager, self.order_book)
        self.journal.remove(entry.client_order_id)
        if entry.attempts > 1:
            self.logger.info(f"Placement {entry.client_order_id} placed after {entry.attempts} attempts.")
        if entry.purpose is PlacementPurpose.PAIRED:
            self._notify(NotificationType.ORDER_PLACED, order_details=str(order))
        return order

    async def _drop_entry(self, entry: PendingPlacement) -> None:
        """Removes a limit entry that will not become an order: releases its funds and frees its level."""
        self.journal.remove(entry.client_order_id)
        await self._release_funds(entry.side, entry.quantity, entry.price)
        grid_level = self.grid_manager.grid_levels.get(entry.level_price)
        if grid_level is not None:
            self.grid_manager.rollback_order_placement(grid_level, entry.side, self._level_busy(grid_level))

    def _level_busy(self, grid_level: GridLevel) -> bool:
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

    def _report_refusal(self, purpose: PlacementPurpose, price: float, error: OrderRefusedError) -> None:
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
                if self._positions_closed:
                    for entry in self.journal.limit_entries():
                        await self._drop_entry(entry)
                    return
                for entry in self.journal.limit_entries():
                    if self.journal.get(entry.client_order_id) is entry:
                        await self._retry_entry(entry)
            finally:
                await self._save_state()

    async def _retry_entry(self, entry: PendingPlacement) -> None:
        try:
            if entry.sent and await self._resolve_sent_entry(entry) is not None:
                return
            await self._send_entry(entry)
        except OrderRefusedError as e:
            if entry.purpose is PlacementPurpose.GRID:
                self._report_unfunded([(entry.side, entry.level_price)])
            else:
                self._report_refusal(entry.purpose, entry.level_price, e)
        except DataFetchError as e:
            self.logger.warning(f"Could not look up placement {entry.client_order_id}; retried at the next poll: {e}")
        except Exception as e:
            details = f"Retry of placement {entry.client_order_id} failed: {e}"
            self.logger.error(details, exc_info=True)
            self._notify(NotificationType.ERROR_OCCURRED, error_details=details)

    async def _resolve_sent_entry(self, entry: PendingPlacement) -> Order | None:
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

    async def _replace_on_level(self, order: Order, grid_level: GridLevel) -> None:
        """Re-places an order that ended unfilled on its level, for the quantity it did not fill."""
        quantity = order.remaining if order.remaining and order.remaining > 0 else order.amount
        try:
            await self._submit_limit(order.side, grid_level, quantity, PlacementPurpose.REPLACEMENT)
        except OrderRefusedError as e:
            self._report_refusal(PlacementPurpose.REPLACEMENT, grid_level.price, e)
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

    def _handle_order_error(self, error: Exception, context: str) -> None:
        if isinstance(error, OrderExecutionFailedError | DataFetchError | OrderRefusedError):
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

    async def apply_order_update(self, update: Order) -> None:
        """
        Applies the exchange's view of one order under the lock: books what it filled, moves its grid level, places
        the paired order, then saves the state. Its notifications go out once the lock is released.
        """
        async with self._locked():
            local = self.order_book.get_order(update.identifier)
            if local is None or not local.is_open():
                self.logger.info(f"Update for order {update.identifier}, which is not open locally; ignoring it.")
                return
            try:
                await self._apply_update(local, update)
            finally:
                await self._save_state()

    async def _apply_update(self, local: Order, update: Order) -> None:
        if update.status == OrderStatus.CLOSED:
            await self._settle_fill(local, update)
        elif update.status in ENDED_UNFILLED_STATUSES:
            await self._settle_end(local, update)
        else:
            self.logger.info(f"Order {update.identifier} reported as {update.status.value}; nothing to apply.")

    async def _settle_end(self, local: Order, update: Order) -> None:
        """
        Books what an order filled before it ended unfilled, frees its level, then re-places it unless the exchange
        rejected it or the positions are closed.
        """
        newly_filled = local.apply_update(update)
        self.order_book.remove_open_order(local)
        await self.balance_tracker.record_execution(local, newly_filled)
        self._notify(
            NotificationType.ORDER_CANCELLED,
            order_details=str(local),
        )
        grid_level = self.order_book.get_grid_level_for_order(local)
        if grid_level is None:
            self.logger.warning(f"Order {local.identifier} ended ({local.status.value}) without a grid level.")
        else:
            self.grid_manager.rollback_order_placement(grid_level, local.side, self._level_busy(grid_level))
        if local.status == OrderStatus.REJECTED:
            self.logger.error(f"The exchange rejected order {local.identifier} at {local.price}; not re-placing it.")
            self._notify(
                NotificationType.ERROR_OCCURRED,
                error_details=f"The exchange rejected order {local.identifier} at {local.price}; it is not re-placed.",
            )
            return
        if grid_level is None:
            return
        if self._positions_closed:
            self.logger.info(f"Positions are closed; not re-placing ended order {local.identifier}.")
            return
        if self._resolving_for_cancel:
            self.logger.info(f"Orders are being cancelled; not re-placing ended order {local.identifier}.")
            return
        await self._replace_on_level(local, grid_level)

    async def _settle_fill(self, local: Order, update: Order) -> None:
        newly_filled = local.apply_update(update)
        self.order_book.remove_open_order(local)
        await self.balance_tracker.record_execution(local, newly_filled)
        self._notify(NotificationType.ORDER_FILLED, order_details=str(local))
        if self._positions_closed:
            self.logger.info(f"Positions already closed; fill of {local.identifier} booked without a paired order.")
            return
        grid_level = self.order_book.get_grid_level_for_order(local)
        if grid_level is None:
            self.logger.error(f"Filled order {local.identifier} has no grid level; no paired order placed.")
            return
        try:
            await self._handle_order_completion(local, grid_level)
        except Exception as e:
            self.logger.error(f"Error while handling filled order {local.identifier}: {e}", exc_info=True)
            # Send errors never reach this clause, so what does is a bug that must fail a backtest run.
            if self.trading_mode == TradingMode.BACKTEST:
                raise
            self._notify(
                NotificationType.ERROR_OCCURRED,
                error_details=f"Failed handling filled order {local.identifier}: {e}",
            )

    async def _handle_order_completion(
        self,
        order: Order,
        grid_level: GridLevel,
    ) -> None:
        """
        Transitions the filled level, then places the paired order on the level the strategy chooses.

        Args:
            order: The filled Order instance.
            grid_level: The grid level associated with the filled order.
        """
        self.logger.info(f"{order.side.value.capitalize()} order completed at grid level {grid_level}.")
        self.grid_manager.complete_order(grid_level, order.side, self._level_busy(grid_level))
        if self._resolving_for_cancel:
            self.logger.info(f"Orders are being cancelled; no paired order placed for {order.identifier}.")
            return
        await self._place_paired_order(order, grid_level)

    async def _place_paired_order(self, filled_order: Order, grid_level: GridLevel) -> None:
        """
        Places the sell for a filled buy, sized on the crypto the buy delivered net of its fee, or the rebuy for a
        filled sell, on the strategy's target level, and reports a refusal.
        """
        if filled_order.side == OrderSide.BUY:
            side, target = OrderSide.SELL, self.grid_manager.get_sell_target(grid_level)
            net = filled_order.filled - self.balance_tracker.fee_calculator.order_base_fee(filled_order)
            quantity = net * self.grid_manager.sell_ratio
        else:
            side, target = OrderSide.BUY, self.grid_manager.get_buy_target(grid_level)
            quantity = filled_order.filled * self.grid_manager.buy_ratio
        if target is None:
            return
        try:
            await self._submit_limit(side, target, quantity, PlacementPurpose.PAIRED, grid_level)
        except OrderRefusedError as e:
            self._report_refusal(PlacementPurpose.PAIRED, target.price, e)

    async def place_paired_orders_for_recovered_fills(
        self,
        filled_orders: list[tuple[Order, GridLevel]],
    ) -> None:
        """
        Places paired orders for orders that filled while the bot was down.

        For each recovered fill, places the corresponding paired order
        (buy fill → sell above, sell fill → buy below) without re-triggering
        grid state transitions (already done during reconciliation).

        Args:
            filled_orders: List of (Order, GridLevel) tuples for orders
                that were found CLOSED on the exchange during recovery.
        """
        async with self._locked():
            try:
                for order, grid_level in filled_orders:
                    try:
                        await self._place_paired_order(order, grid_level)
                    except Exception as e:
                        self._handle_order_error(e, f"Error placing paired order for recovered fill {order.identifier}")
            finally:
                await self._save_state()

    async def perform_initial_purchase(self, current_price: float) -> bool:
        """
        Buys, once, the crypto the grid's sells need. Returns whether the purchase is done; it is not while the
        exchange cannot take or confirm the order, and the caller tries again at its next tick.
        """
        async with self._locked():
            if self.arming.initial_purchase_done:
                self.logger.info("Initial purchase already done; skipping it.")
                return True
            done = False
            try:
                if self._positions_closed:
                    self.logger.info("Positions are closed; skipping the initial purchase.")
                    done = True
                else:
                    done = await self._buy_initial_position(current_price)
                return done
            finally:
                if done:
                    self.arming = replace(self.arming, initial_purchase_done=True)
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
            if self.market_constraints is not None:
                violation = self.market_constraints.violation(quantity, current_price)
                if violation:
                    self.logger.warning(f"Skipping initial purchase: {violation}")
                    self._notify(NotificationType.ORDER_FAILED, error_details=f"Initial purchase skipped: {violation}")
                    return True
            try:
                entry = self._new_market_entry(
                    OrderSide.BUY, quantity, current_price, PlacementPurpose.INITIAL_PURCHASE
                )
            except OrderRefusedError as e:
                self.logger.warning(f"Skipping initial purchase: {e}")
                self._notify(NotificationType.ORDER_FAILED, error_details=f"Initial purchase skipped: {e}")
                return True

        for _ in range(self.MAX_MARKET_LEGS):
            self.logger.info(f"Performing initial crypto purchase: {entry.quantity} at price {current_price}.")
            try:
                order = await self._send_market_entry(entry)
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
            self.journal.remove(entry.client_order_id)
            if not order.filled:
                self.logger.warning("The initial purchase leg filled nothing; retried at the next tick.")
                return False
            await self._book_initial_purchase(order)
            remaining = entry.quantity - order.filled
            if self._is_dust(remaining, current_price):
                return True
            try:
                entry = self._new_market_entry(
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

    def _new_market_entry(
        self, side: OrderSide, quantity: float, price: float, purpose: PlacementPurpose
    ) -> PendingPlacement:
        entry = PendingPlacement(
            client_order_id=new_client_order_id(self.config_hash),
            order_type=OrderType.MARKET,
            side=side,
            quantity=self.order_execution_strategy.round_amount(self.trading_pair, quantity),
            price=price,
            purpose=purpose,
            created_at=int(time.time() * 1000),
        )
        self.journal.add(entry)
        return entry

    async def _send_market_entry(self, entry: PendingPlacement) -> Order:
        """
        Sends one market leg through the journal, looking it up first when an earlier send's outcome is unknown.
        A found order is returned only once it is closed and filled; one that ended unfilled has taken its client id, so
        the entry moves to a new one. A lookup counts as an attempt.
        Raises DataFetchError when the order is not placed or its outcome stays unknown, OrderRefusedError on a refusal.
        """
        if entry.sent:
            entry.attempts += 1
            found = await self.order_execution_strategy.find_order(entry.client_order_id, self.trading_pair)
            if found is not None and found.status == OrderStatus.OPEN:
                raise OrderOutcomeUnknownError(f"Market order {entry.client_order_id} is still open on the exchange.")
            if found is not None and found.filled:
                settled = copy.copy(found)
                settled.status = OrderStatus.CLOSED
                settled.average = found.average or found.price or entry.price
                return settled
            if found is not None:
                self.journal.remove(entry.client_order_id)
                entry.client_order_id = new_client_order_id(self.config_hash)
                entry.attempts = 0
                self.journal.add(entry)
            entry.sent = False
        entry.sent = True
        if self.state_store is not None and not await self.state_store.save_pending(entry):
            entry.sent = False
            raise OrderNotPlacedError(f"Placement {entry.client_order_id} could not be saved before sending.")
        entry.attempts += 1
        try:
            return await self.order_execution_strategy.execute_market_order(
                entry.side, self.trading_pair, entry.quantity, entry.price, entry.client_order_id
            )
        except OrderNotPlacedError:
            entry.sent = False
            raise

    async def _book_initial_purchase(self, order: Order) -> None:
        # A leg whose remainder was cancelled is final for what it filled; the next leg buys the rest.
        order.status = OrderStatus.CLOSED
        self.logger.info(f"Initial crypto purchase completed. Order details: {order}")
        self.order_book.add_order(order)
        self._notify(NotificationType.ORDER_PLACED, order_details=f"Initial purchase done: {order!s}")
        await self.balance_tracker.update_after_initial_purchase(initial_order=order)

    def _is_dust(self, quantity: float, price: float) -> bool:
        if quantity <= 0:
            return True
        return self.market_constraints is not None and self.market_constraints.violation(quantity, price) is not None

    async def close_positions(self, liquidate: bool, current_price: float | None = None) -> None:
        """
        Unified shutdown path: cancels every open grid order (releasing reservations),
        then optionally liquidates the full crypto balance at market. Idempotent —
        a second call (e.g. STOP_BOT after a TP liquidation) is a no-op.

        Note: `cancel_open_grid_orders` acquires `self._lock` itself, so it must be
        awaited outside any lock held by this method to avoid deadlocking.
        """
        if self._positions_closed:
            self.logger.info("close_positions already executed; skipping.")
            return
        self._positions_closed = True

        cancelled = await self.cancel_open_grid_orders()
        if not cancelled:
            self.logger.error("Could not cancel all open grid orders during shutdown.")
            await self.notification_handler.async_send_notification(
                NotificationType.ERROR_OCCURRED,
                error_details="Shutdown: some open grid orders could not be cancelled on the exchange.",
            )

        if not liquidate:
            return

        async with self._locked():
            try:
                quantity = self.balance_tracker.crypto_balance
                if quantity <= 0:
                    self.logger.info("No crypto balance to liquidate.")
                    return
                if self.market_constraints is not None and current_price is not None:
                    violation = self.market_constraints.violation(quantity, current_price)
                    if violation:
                        self.logger.error(f"Cannot liquidate position: {violation}")
                        self._notify(
                            NotificationType.ERROR_OCCURRED,
                            error_details=f"Liquidation skipped: {violation}",
                        )
                        return
                try:
                    order = await self.order_execution_strategy.execute_market_order(
                        OrderSide.SELL,
                        self.trading_pair,
                        quantity,
                        current_price,
                        client_order_id=new_client_order_id(self.config_hash),
                    )
                    if not order:
                        raise OrderExecutionFailedError(f"Liquidation order returned None at price {current_price}")
                    await self.balance_tracker.update_after_liquidation(order)
                    self.order_book.add_order(order)
                    if order.filled < quantity:
                        self._notify(
                            NotificationType.ERROR_OCCURRED,
                            error_details=(
                                f"Liquidation partially filled: {order.filled}/{quantity}. "
                                f"Remaining position is still on the exchange account."
                            ),
                        )
                except OrderExecutionFailedError as e:
                    self.logger.error(f"Liquidation failed: {e!s}")
                    self._notify(
                        NotificationType.ORDER_FAILED,
                        error_details=f"Failed to liquidate position: {e}",
                    )
                except (DataFetchError, OrderRefusedError, ValueError) as e:
                    self.logger.error(f"Failed to liquidate at {current_price}: {e}")
                    self._notify(
                        NotificationType.ERROR_OCCURRED,
                        error_details=f"Failed to liquidate position: {e}",
                    )
            finally:
                await self._save_state()

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
        await self.notification_handler.async_send_notification(
            NotificationType.TAKE_PROFIT_TRIGGERED if take_profit_order else NotificationType.STOP_LOSS_TRIGGERED,
            order_details=f"{event} at {current_price}: open orders cancelled, position liquidated.",
        )
        self.logger.info(f"{event} triggered at {current_price}; close_positions executed.")
