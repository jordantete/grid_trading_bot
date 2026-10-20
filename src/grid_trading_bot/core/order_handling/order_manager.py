import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
import logging
from typing import Any

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.exceptions import OrderRefusedError
from grid_trading_bot.core.services.exceptions import DataFetchError, PostOnlyRejectedError
from grid_trading_bot.core.services.market_constraints import MarketConstraints

from ..grid_management.grid_level import GridLevel
from ..grid_management.grid_manager import GridManager
from ..order_handling.balance_tracker import BalanceTracker
from ..order_handling.order_book import OrderBook
from ..validation.exceptions import (
    InsufficientBalanceError,
    InsufficientCryptoBalanceError,
)
from ..validation.order_validator import OrderValidator
from .exceptions import GridFeasibilityError, OrderExecutionFailedError
from .execution_strategy.order_execution_strategy_interface import (
    OrderExecutionStrategyInterface,
)
from .order import ENDED_UNFILLED_STATUSES, Order, OrderSide, OrderStatus
from .placement_journal import PlacementJournal, new_client_order_id
from .state_store import ArmingFlags, StateStore


class OrderManager:
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
        for grid_level, side in self.grid_manager.get_initial_order_plan(current_price):
            if self._positions_closed:
                self.logger.info("Positions are closed; skipping the remaining initial grid orders.")
                return
            await self._initialize_order(grid_level, side, current_price)

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
                    grid_level, order.side, self.order_book.has_open_orders_at(grid_level)
                )
            self.logger.info(f"Cancelled {order.side.value} order {order.identifier} at {order.price}.")

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

    async def _initialize_order(self, grid_level: GridLevel, side: OrderSide, current_price: float) -> None:
        price = grid_level.price
        total_balance_value = self.balance_tracker.get_total_balance_value(current_price)
        order_quantity = self.grid_manager.get_order_size_for_grid_level(total_balance_value, current_price, side)

        if not self.grid_manager.can_place_order(grid_level, side):
            return

        try:
            adjusted_quantity = self._validate_order_quantity(side, order_quantity, price)

            if self.market_constraints is not None:
                violation = self.market_constraints.violation(adjusted_quantity, price)
                if violation:
                    self.logger.warning(f"Skipping initial {side.value} order at {price}: {violation}")
                    return

            await self._reserve_funds(side, adjusted_quantity, price)

            self.logger.info(
                f"Placing initial {side.value} limit order at grid level {price} for "
                f"{adjusted_quantity} {self.trading_pair}.",
            )

            try:
                order = await self.order_execution_strategy.execute_limit_order(
                    side,
                    self.trading_pair,
                    adjusted_quantity,
                    price,
                    client_order_id=new_client_order_id(self.config_hash),
                )

                if order is None:
                    await self._release_funds(side, adjusted_quantity, price)
                    raise OrderExecutionFailedError(
                        f"{side.value.capitalize()} order at {price} returned None",
                    )
            except PostOnlyRejectedError as e:
                await self._skip_post_only_rejection(side, adjusted_quantity, price, e, "Initial grid placement")
                return
            except Exception:
                await self._release_funds(side, adjusted_quantity, price)
                raise

            order = self._rest(order)
            self.grid_manager.mark_order_pending(grid_level, order)
            self.order_book.add_order(order, grid_level)

        except (OrderExecutionFailedError, Exception) as e:
            self._handle_order_error(e, f"Error while placing initial {side.value} order")

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

    async def _skip_post_only_rejection(
        self,
        side: OrderSide,
        quantity: float,
        price: float,
        error: PostOnlyRejectedError,
        context: str,
    ) -> None:
        """
        Handles a maker-only order the exchange refused because it would have crossed.

        This is an expected market condition, not a failure: the reservation is released and
        the grid level is left exactly as it was, so the next `initialize_grid_orders` (a
        regrid or trailing-stop move) retries it. Logged at INFO with no failure
        notification — reporting it as an error would drown the real ones.
        """
        await self._release_funds(side, quantity, price)
        self.logger.info(
            f"{context}: maker-only {side.value} order at grid level {price} would have crossed "
            f"and was not placed ({error}). Level left available for a later cycle.",
        )

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
                if update.status == OrderStatus.CLOSED:
                    await self._settle_fill(local, update)
                elif update.status in ENDED_UNFILLED_STATUSES:
                    await self._settle_end(local, update)
                else:
                    self.logger.info(f"Order {update.identifier} reported as {update.status.value}; nothing to apply.")
            finally:
                await self._save_state()

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
            self.grid_manager.rollback_order_placement(
                grid_level, local.side, self.order_book.has_open_orders_at(grid_level)
            )
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
        await self._replace_ended_order(local, grid_level)

    async def _replace_ended_order(self, order: Order, grid_level: GridLevel) -> None:
        try:
            quantity = order.remaining if order.remaining and order.remaining > 0 else order.amount
            adjusted_quantity = self._validate_order_quantity(order.side, quantity, grid_level.price)
            if self.market_constraints is not None:
                violation = self.market_constraints.violation(adjusted_quantity, grid_level.price)
                if violation:
                    self.logger.warning(f"Not re-placing ended order at {grid_level.price}: {violation}")
                    return
            await self._reserve_funds(order.side, adjusted_quantity, grid_level.price)
            try:
                new_order = await self.order_execution_strategy.execute_limit_order(
                    order.side,
                    self.trading_pair,
                    adjusted_quantity,
                    grid_level.price,
                    client_order_id=new_client_order_id(self.config_hash),
                )
            except PostOnlyRejectedError as e:
                await self._skip_post_only_rejection(
                    order.side, adjusted_quantity, grid_level.price, e, "Ended-order re-placement"
                )
                return
            except Exception:
                await self._release_funds(order.side, adjusted_quantity, grid_level.price)
                raise
            if new_order is None:
                await self._release_funds(order.side, adjusted_quantity, grid_level.price)
                self.logger.error(f"Re-placement at grid level {grid_level.price} returned None.")
                return
            new_order = self._rest(new_order)
            self.grid_manager.mark_order_pending(grid_level, new_order)
            self.order_book.add_order(new_order, grid_level)
            self.logger.info(f"Re-placed ended {order.side.value} order at grid level {grid_level.price}.")
        except Exception as e:
            self._handle_order_error(e, f"Failed to re-place ended order at grid level {grid_level.price}")

    @staticmethod
    def _rest(order: Order) -> Order:
        """A placed limit order enters the book open with nothing booked; any fill it already has reaches the
        pipeline through the tracker."""
        order.status = OrderStatus.OPEN
        order.filled = 0.0
        order.remaining = order.amount
        return order

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
        except OrderExecutionFailedError as e:
            self.logger.error(f"Failed while handling filled order - {e!s}", exc_info=True)
            self._notify(
                NotificationType.ORDER_FAILED,
                error_details=f"Failed handling filled order. {e}",
            )
        except (DataFetchError, ValueError) as e:
            self.logger.error(f"Error while handling filled order {local.identifier}: {e}", exc_info=True)
            self._notify(
                NotificationType.ORDER_FAILED,
                error_details=f"Failed handling filled order. {e}",
            )
        except OrderRefusedError as e:
            self.logger.error(f"Paired order for filled order {local.identifier} refused: {e}")
            self._notify(
                NotificationType.ORDER_FAILED,
                error_details=f"Failed handling filled order. {e}",
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
        self.grid_manager.complete_order(grid_level, order.side, self.order_book.has_open_orders_at(grid_level))
        await self._place_paired_order(order, grid_level)

    async def _place_paired_order(self, filled_order: Order, grid_level: GridLevel) -> None:
        """
        Places the sell for a filled buy, or the rebuy for a filled sell, on the strategy's target level.
        """
        if filled_order.side == OrderSide.BUY:
            target = self.grid_manager.get_sell_target(grid_level)
            if target is not None:
                received = filled_order.filled - self.balance_tracker.fee_calculator.order_base_fee(filled_order)
                await self._place_order(OrderSide.SELL, grid_level, target, received * self.grid_manager.sell_ratio)
        else:
            target = self.grid_manager.get_buy_target(grid_level)
            if target is not None:
                quantity = filled_order.filled * self.grid_manager.buy_ratio
                await self._place_order(OrderSide.BUY, grid_level, target, quantity)

    async def _place_order(
        self,
        order_side: OrderSide,
        source_grid_level: GridLevel,
        target_grid_level: GridLevel,
        quantity: float,
    ) -> None:
        """
        Places an order at the specified target grid level and pairs it with the source level.

        Args:
            order_side: The side of the order (BUY or SELL).
            source_grid_level: The grid level that triggered this order.
            target_grid_level: The grid level to place the order on.
            quantity: The quantity of the order.
        """
        if self._positions_closed:
            self.logger.info(
                f"Positions are closed; not placing {order_side.value} order at {target_grid_level.price}."
            )
            return

        adjusted_quantity = self._validate_order_quantity(order_side, quantity, target_grid_level.price)

        if self.market_constraints is not None:
            violation = self.market_constraints.violation(adjusted_quantity, target_grid_level.price)
            if violation:
                self.logger.warning(
                    f"Skipping {order_side.value} order at {target_grid_level.price}: {violation}",
                )
                self._notify(
                    NotificationType.ORDER_FAILED,
                    error_details=f"Order at {target_grid_level.price} skipped: {violation}",
                )
                return

        try:
            await self._reserve_funds(order_side, adjusted_quantity, target_grid_level.price)
        except (InsufficientBalanceError, InsufficientCryptoBalanceError) as e:
            self.logger.error(
                f"Failed to reserve funds for {order_side.value} order at {target_grid_level.price}: {e}",
            )
            return

        try:
            order = await self.order_execution_strategy.execute_limit_order(
                order_side,
                self.trading_pair,
                adjusted_quantity,
                target_grid_level.price,
                client_order_id=new_client_order_id(self.config_hash),
            )
        except PostOnlyRejectedError as e:
            await self._skip_post_only_rejection(
                order_side, adjusted_quantity, target_grid_level.price, e, "Paired order placement"
            )
            return
        except Exception:
            await self._release_funds(order_side, adjusted_quantity, target_grid_level.price)
            raise

        if order:
            self._rest(order)
            pairing_type = "buy" if order_side == OrderSide.BUY else "sell"
            self.grid_manager.pair_grid_levels(source_grid_level, target_grid_level, pairing_type=pairing_type)
            self.grid_manager.mark_order_pending(target_grid_level, order)
            self.order_book.add_order(order, target_grid_level)
            self._notify(
                NotificationType.ORDER_PLACED,
                order_details=str(order),
            )
        else:
            await self._release_funds(order_side, adjusted_quantity, target_grid_level.price)
            self.logger.error(
                f"Failed to place {order_side.value} order at grid level {target_grid_level}",
            )

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

    async def perform_initial_purchase(
        self,
        current_price: float,
    ) -> None:
        """
        Handles the initial crypto purchase for grid trading strategy if required.

        Args:
            current_price: The current price of the trading pair.
        """
        async with self._locked():
            if self.arming.initial_purchase_done:
                self.logger.info("Initial purchase already done; skipping it.")
                return
            try:
                if self._positions_closed:
                    self.logger.info("Positions are closed; skipping the initial purchase.")
                    return
                await self._buy_initial_position(current_price)
            finally:
                self.arming = replace(self.arming, initial_purchase_done=True)
                await self._save_state()

    async def _buy_initial_position(self, current_price: float) -> None:
        initial_quantity = self.grid_manager.get_initial_order_quantity(
            current_fiat_balance=self.balance_tracker.balance,
            current_crypto_balance=self.balance_tracker.crypto_balance,
            current_price=current_price,
        )

        if initial_quantity <= 0:
            self.logger.warning("Initial purchase quantity is zero or negative. Skipping initial purchase.")
            return

        if self.market_constraints is not None:
            violation = self.market_constraints.violation(initial_quantity, current_price)
            if violation:
                self.logger.warning(f"Skipping initial purchase: {violation}")
                self._notify(
                    NotificationType.ORDER_FAILED,
                    error_details=f"Initial purchase skipped: {violation}",
                )
                return

        self.logger.info(f"Performing initial crypto purchase: {initial_quantity} at price {current_price}.")

        try:
            buy_order = await self.order_execution_strategy.execute_market_order(
                OrderSide.BUY,
                self.trading_pair,
                initial_quantity,
                current_price,
                client_order_id=new_client_order_id(self.config_hash),
            )
            self.logger.info(f"Initial crypto purchase completed. Order details: {buy_order}")
            self.order_book.add_order(buy_order)
            self._notify(
                NotificationType.ORDER_PLACED,
                order_details=f"Initial purchase done: {buy_order!s}",
            )

            await self.balance_tracker.update_after_initial_purchase(initial_order=buy_order)

        except OrderExecutionFailedError as e:
            self.logger.error(f"Failed while executing initial purchase - {e!s}", exc_info=True)
            self._notify(
                NotificationType.ORDER_FAILED,
                error_details=f"Error while performing initial purchase. {e}",
            )

        except (DataFetchError, OrderRefusedError, ValueError) as e:
            self.logger.error(
                f"Failed to perform initial purchase at current_price: {current_price} - error: {e}",
                exc_info=True,
            )
            self._notify(
                NotificationType.ORDER_FAILED,
                error_details=f"Error while performing initial purchase. {e}",
            )

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
