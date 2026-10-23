import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
import logging
from typing import Any

from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.exceptions import OrderRefusedError
from grid_trading_bot.core.services.exceptions import DataFetchError
from grid_trading_bot.core.services.market_constraints import MarketConstraints

from ..grid_management.grid_level import GridLevel
from ..grid_management.grid_manager import GridManager
from ..order_handling.balance_tracker import BalanceTracker
from ..order_handling.order_book import OrderBook
from ..validation.order_validator import OrderValidator
from .exceptions import GridFeasibilityError
from .execution_strategy.order_execution_strategy_interface import OrderExecutionStrategyInterface
from .initial_purchase import InitialPurchase
from .liquidation import Liquidation
from .market_entry import MarketEntrySender
from .order import ENDED_UNFILLED_STATUSES, Order, OrderSide, OrderStatus
from .placement import Placement
from .placement_journal import PendingPlacement, PlacementJournal, PlacementPurpose
from .state_store import ArmingFlags, StateStore


class OrderManager:
    """Runs every order operation under one lock: applies order updates and delegates the rest to collaborators."""

    def __init__(
        self,
        grid_manager: GridManager,
        order_validator: OrderValidator,
        balance_tracker: BalanceTracker,
        order_book: OrderBook,
        order_execution_strategy: OrderExecutionStrategyInterface,
        notification_handler: NotificationHandler,
        fail_fast: bool,
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
        self.fail_fast = fail_fast
        self.trading_pair = trading_pair
        self.state_store = state_store
        self.journal = journal if journal is not None else PlacementJournal()
        self.config_hash = config_hash
        self.arming = ArmingFlags()
        self.market_constraints: MarketConstraints | None = None
        self._lock = asyncio.Lock()
        self._outbox: list[tuple[NotificationType, dict[str, Any]]] = []
        self._resolving_for_cancel = False
        self.exchange_balance: Callable[[], Awaitable[float]] | None = None
        market_entries = MarketEntrySender(
            self.journal, order_execution_strategy, trading_pair, config_hash, self._save_pending
        )
        self.liquidation = Liquidation(
            balance_tracker=balance_tracker,
            order_book=order_book,
            journal=self.journal,
            order_execution_strategy=order_execution_strategy,
            market_entries=market_entries,
            notification_handler=notification_handler,
            trading_pair=trading_pair,
            fail_fast=fail_fast,
            notify=self._notify,
            send_now=self._send,
            locked=self._locked,
            save_state=self._save_state,
            cancel_grid_orders=lambda: self.cancel_open_grid_orders(),
            market_constraints=lambda: self.market_constraints,
            exchange_balance=lambda: self.exchange_balance,
        )
        self.placement = Placement(
            grid_manager=grid_manager,
            order_validator=order_validator,
            balance_tracker=balance_tracker,
            order_book=order_book,
            order_execution_strategy=order_execution_strategy,
            journal=self.journal,
            trading_pair=trading_pair,
            config_hash=config_hash,
            notify=self._notify,
            locked=self._locked,
            save_state=self._save_state,
            save_pending=self._save_pending,
            market_constraints=lambda: self.market_constraints,
            positions_closed=lambda: self.positions_closed,
            apply_update=self._apply_update,
            report_unfunded=self._report_unfunded,
        )
        self.initial_purchase = InitialPurchase(
            grid_manager=grid_manager,
            balance_tracker=balance_tracker,
            order_book=order_book,
            journal=self.journal,
            market_entries=market_entries,
            notify=self._notify,
            locked=self._locked,
            save_state=self._save_state,
            market_constraints=lambda: self.market_constraints,
            positions_closed=lambda: self.positions_closed,
            purchase_done=lambda: self.arming.initial_purchase_done,
            mark_purchase_done=self._mark_initial_purchase_done,
        )

    @property
    def positions_closed(self) -> bool:
        """Whether `close_positions` has run: no order is placed from then on."""
        return self.liquidation.positions_closed

    @property
    def liquidated(self) -> bool:
        """Whether a liquidation ran to its end, with nothing more it could sell."""
        return self.liquidation.liquidated

    def set_market_constraints(self, constraints: MarketConstraints) -> None:
        self.market_constraints = constraints

    def restore_arming(self, arming: ArmingFlags) -> None:
        self.arming = arming

    def _mark_initial_purchase_done(self) -> None:
        self.arming = replace(self.arming, initial_purchase_done=True)

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

    async def _save_pending(self, entry: PendingPlacement) -> bool:
        """Saves `entry` before it is sent; True without a store."""
        return self.state_store is None or await self.state_store.save_pending(entry)

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

    async def initialize_grid_orders(self, current_price: float):
        """Places the initial grid orders the strategy plans for the current price, then saves the state."""
        async with self._locked():
            try:
                await self._place_initial_orders(current_price)
                self.arming = replace(self.arming, grid_orders_initialized=True)
            finally:
                await self._save_state()

    async def _place_initial_orders(self, current_price: float) -> None:
        unfunded: list[tuple[OrderSide, float]] = []
        for grid_level, side in self.grid_manager.get_initial_order_plan(current_price):
            if self.positions_closed:
                self.logger.info("Positions are closed; skipping the remaining initial grid orders.")
                break
            if not self.grid_manager.can_place_order(grid_level, side):
                continue
            total_balance_value = self.balance_tracker.get_total_balance_value(current_price)
            quantity = self.grid_manager.get_order_size_for_grid_level(total_balance_value, current_price, side)
            try:
                await self.placement.submit_limit(side, grid_level, quantity, PlacementPurpose.GRID)
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
        Cancels all open grid orders, releases their reserved funds and resets their grid level states so orders can be
        re-placed. Returns True only if every open order was cancelled; on the first failure it stops and returns
        False, leaving remaining orders open.
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
                    await self.placement.resolve_sent_entry(entry)
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
                self.grid_manager.rollback_order_placement(
                    grid_level, order.side, self.placement.level_busy(grid_level)
                )
            self.logger.info(f"Cancelled {order.side.value} order {order.identifier} at {order.price}.")

        for entry in self.journal.limit_entries():
            await self.placement.drop_entry(entry)
        return True

    async def _refetch_cancelled(self, order: Order) -> Order | None:
        """The exchange's view of an order just cancelled; None when the exchange has no view of it or it cannot be
        read."""
        try:
            return await self.order_execution_strategy.get_order(order.identifier, self.trading_pair)
        except DataFetchError as e:
            self.logger.warning(f"Could not refetch cancelled order {order.identifier}; using local state: {e}")
            return None

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
        self._notify(NotificationType.ORDER_CANCELLED, order_details=str(local))
        grid_level = self.order_book.get_grid_level_for_order(local)
        if grid_level is None:
            self.logger.warning(f"Order {local.identifier} ended ({local.status.value}) without a grid level.")
        else:
            self.grid_manager.rollback_order_placement(grid_level, local.side, self.placement.level_busy(grid_level))
        if local.status == OrderStatus.REJECTED:
            self.logger.error(f"The exchange rejected order {local.identifier} at {local.price}; not re-placing it.")
            self._notify(
                NotificationType.ERROR_OCCURRED,
                error_details=f"The exchange rejected order {local.identifier} at {local.price}; it is not re-placed.",
            )
            return
        if grid_level is None:
            return
        if self.positions_closed:
            self.logger.info(f"Positions are closed; not re-placing ended order {local.identifier}.")
            return
        if self._resolving_for_cancel:
            self.logger.info(f"Orders are being cancelled; not re-placing ended order {local.identifier}.")
            return
        await self.placement.replace_on_level(local, grid_level)

    async def _settle_fill(self, local: Order, update: Order) -> None:
        newly_filled = local.apply_update(update)
        self.order_book.remove_open_order(local)
        await self.balance_tracker.record_execution(local, newly_filled)
        self._notify(NotificationType.ORDER_FILLED, order_details=str(local))
        if self.positions_closed:
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
            # Send errors never reach this clause, so what does is a bug that must fail a fail-fast run.
            if self.fail_fast:
                raise
            self._notify(
                NotificationType.ERROR_OCCURRED,
                error_details=f"Failed handling filled order {local.identifier}: {e}",
            )

    async def _handle_order_completion(self, order: Order, grid_level: GridLevel) -> None:
        """Transitions the filled level, then places the paired order on the level the strategy chooses."""
        self.logger.info(f"{order.side.value.capitalize()} order completed at grid level {grid_level}.")
        self.grid_manager.complete_order(grid_level, order.side, self.placement.level_busy(grid_level))
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
            await self.placement.submit_limit(
                side, target, quantity, PlacementPurpose.PAIRED, grid_level, source_order_id=filled_order.identifier
            )
        except OrderRefusedError as e:
            self.placement.report_refusal(PlacementPurpose.PAIRED, target.price, e)

    async def place_paired_orders_for_recovered_fills(self, filled_orders: list[tuple[Order, GridLevel]]) -> None:
        """
        Places the paired order (buy fill → sell above, sell fill → buy below) of each order recovery found CLOSED on
        the exchange, without re-triggering grid state transitions (already done during reconciliation).
        """
        async with self._locked():
            try:
                for order, grid_level in filled_orders:
                    try:
                        await self._place_paired_order(order, grid_level)
                    except Exception as e:
                        self.placement.handle_order_error(
                            e, f"Error placing paired order for recovered fill {order.identifier}"
                        )
            finally:
                await self._save_state()

    async def place_replacements_for_recovered_ends(self, ended: list[tuple[Order, GridLevel]]) -> None:
        """Re-places each order cancelled or expired while the bot was down, on its level, for its unfilled quantity."""
        async with self._locked():
            try:
                for order, grid_level in ended:
                    await self.placement.replace_on_level(order, grid_level)
            finally:
                await self._save_state()

    async def retry_pending_placements(self) -> None:
        """Resolves or sends again, in creation order, every limit placement the journal holds, then saves."""
        await self.placement.retry_pending_placements()

    async def perform_initial_purchase(self, current_price: float) -> bool:
        """Buys, once, the crypto the grid's sells need; returns whether the purchase is done."""
        return await self.initial_purchase.perform(current_price)

    async def close_positions(self, liquidate: bool, current_price: float | None = None) -> None:
        """Cancels the grid orders once, then sells the crypto when asked; awaited outside the order lock."""
        await self.liquidation.close_positions(liquidate, current_price)

    def interrupt_liquidation(self) -> None:
        """Ends a liquidation's retries; the attempt in progress, or a first one, still runs."""
        self.liquidation.interrupt()

    async def execute_take_profit_or_stop_loss_order(
        self, current_price: float, take_profit_order: bool = False, stop_loss_order: bool = False
    ) -> None:
        """Closes the position after a take-profit or stop-loss event, then reports how the liquidation ended."""
        await self.liquidation.execute_take_profit_or_stop_loss_order(current_price, take_profit_order, stop_loss_order)
