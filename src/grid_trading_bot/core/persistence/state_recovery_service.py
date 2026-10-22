import copy
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
import json
import logging

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.grid_management.grid_level import GridCycleState, GridLevel
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.order_handling.balance_tracker import _QUANTIZE_EXP, BalanceTracker
from grid_trading_bot.core.order_handling.execution_strategy.order_execution_strategy_interface import (
    OrderExecutionStrategyInterface,
)
from grid_trading_bot.core.order_handling.order import (
    ENDED_UNFILLED_STATUSES,
    FINAL_STATUSES,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.core.order_handling.placement_journal import (
    PendingPlacement,
    PlacementJournal,
    PlacementPurpose,
    adopt_order,
    client_order_id_prefix,
)
from grid_trading_bot.core.services.exceptions import DataFetchError, OrderNotFoundError, OrderOutcomeUnknownError
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface

from .serializers import compute_config_hash, dict_to_order, dict_to_pending_placement
from .state_repository_interface import SavedBalance, StateRepositoryInterface


class RecoveryOutcome(Enum):
    RESUMED = "resumed"
    NO_STATE = "no_state"
    REFUSED = "refused"
    DEFERRED = "deferred"


@dataclass
class RecoveryResult:
    outcome: RecoveryOutcome
    reason: str | None = None
    initial_purchase_done: bool = False
    grid_orders_initialized: bool = False
    orders_reconciled: int = 0
    orphan_orders_found: int = 0
    ghost_orders_found: int = 0
    orders_filled_while_down: list[tuple[Order, GridLevel]] = field(default_factory=list)
    ended_while_down: list[tuple[Order, GridLevel]] = field(default_factory=list)
    not_replaced: list[str] = field(default_factory=list)
    liquidation_in_progress: bool = False
    balance_source: str = "db"
    strategy_state: dict | None = None

    @property
    def recovered(self) -> bool:
        return self.outcome is RecoveryOutcome.RESUMED


@dataclass
class _Reconciliation:
    reconciled: int = 0
    orphans: int = 0
    ghosts: int = 0
    filled_while_down: list[tuple[Order, GridLevel]] = field(default_factory=list)
    executions: list[tuple[Order, float]] = field(default_factory=list)
    ended_while_down: list[tuple[Order, GridLevel]] = field(default_factory=list)
    not_replaced: list[str] = field(default_factory=list)
    initial_purchase_found: bool = False
    # Orders whose paired order or replacement a restored placement already is.
    followed: set[str] = field(default_factory=set)


def _optional_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _quantized(value: Decimal) -> Decimal:
    return value.quantize(_QUANTIZE_EXP)


class StateRecoveryService:
    def __init__(
        self,
        repository: StateRepositoryInterface,
        config_manager: ConfigManager,
        grid_manager: GridManager,
        order_book: OrderBook,
        balance_tracker: BalanceTracker,
        exchange_service: ExchangeInterface,
        order_execution_strategy: OrderExecutionStrategyInterface,
        notification_handler: NotificationHandler,
        trading_pair: str,
        journal: PlacementJournal | None = None,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.repository = repository
        self.config_manager = config_manager
        self.grid_manager = grid_manager
        self.order_book = order_book
        self.balance_tracker = balance_tracker
        self.exchange_service = exchange_service
        self.order_execution_strategy = order_execution_strategy
        self.notification_handler = notification_handler
        self.trading_pair = trading_pair
        self.journal = journal if journal is not None else PlacementJournal()

    async def attempt_recovery(self) -> RecoveryResult:
        """
        Loads the saved state and reconciles it with the exchange, without ever writing the database: an exchange
        error defers the start, any other failure refuses it.
        """
        try:
            return await self._do_recovery()
        except DataFetchError as e:
            self.logger.warning(f"Exchange unavailable during recovery: {e}")
            return RecoveryResult(
                RecoveryOutcome.DEFERRED, reason=f"the exchange could not be reached during recovery ({e})"
            )
        except Exception as e:
            self.logger.error(f"Recovery failed with unexpected error: {e}", exc_info=True)
            return RecoveryResult(
                RecoveryOutcome.REFUSED, reason=f"recovery of the saved state failed ({type(e).__name__}: {e})"
            )

    async def _do_recovery(self) -> RecoveryResult:
        bot_state = self.repository.load_bot_state()
        if bot_state is None:
            self.logger.info("No previous state found. Starting fresh.")
            return RecoveryResult(RecoveryOutcome.NO_STATE)

        # Step 1: Config compatibility
        current_hash = compute_config_hash(self.config_manager.config)
        if bot_state["config_hash"] != current_hash:
            self.logger.warning("Config hash mismatch: the grid configuration changed since the last run.")
            return RecoveryResult(
                RecoveryOutcome.REFUSED, reason="the saved state was written for another grid configuration"
            )

        self.logger.info("Previous state found. Attempting recovery...")

        # Parse persisted strategy state early: a runtime regrid changes the grid geometry,
        # so the checkpointed geometry must be re-applied BEFORE matching persisted levels/orders
        # by price (otherwise they would fail to match the config-derived grid).
        strategy_state = self._parse_strategy_state(bot_state.get("strategy_state"))
        if strategy_state:
            price_grids = strategy_state.get("price_grids")
            central_price = strategy_state.get("central_price")
            if price_grids and central_price is not None:
                self.grid_manager.apply_geometry(price_grids, central_price, strategy_state.get("atr_grid"))
                self.logger.info("Restored regridded grid geometry from checkpoint before level/order matching.")

        # Step 2: Restore grid levels
        self._restore_grid_levels()

        # Step 3: Restore orders + OrderBook, then the placements no save resolved
        self._restore_orders()
        liquidation_in_progress = self._restore_journal()

        # Step 4: Reconcile with exchange (updates statuses, completes grid states for filled orders, resolves the
        # placements already sent)
        reconciliation = await self._reconcile_with_exchange(current_hash)

        # Step 5: Restore balance (reserved recalculated from the open orders and the pending placements)
        balance_source = await self._restore_balance(reconciliation.executions)

        # Step 6: Restore cost basis, realized PnL and peak, then book the fills made while down
        if balance_source == "exchange":
            try:
                self._restore_accounting(reconciliation.executions)
            except Exception as e:
                # Reporting-only state: losing it must not turn a good recovery into a fresh start.
                self.logger.error(
                    f"Failed to restore or replay the cost basis and PnL; they may be incomplete: {e}", exc_info=True
                )

        result = RecoveryResult(
            RecoveryOutcome.RESUMED,
            initial_purchase_done=bot_state["initial_purchase_done"] or reconciliation.initial_purchase_found,
            grid_orders_initialized=bot_state["grid_orders_initialized"],
            orders_reconciled=reconciliation.reconciled,
            orphan_orders_found=reconciliation.orphans,
            ghost_orders_found=reconciliation.ghosts,
            orders_filled_while_down=reconciliation.filled_while_down,
            ended_while_down=reconciliation.ended_while_down,
            not_replaced=reconciliation.not_replaced,
            liquidation_in_progress=liquidation_in_progress,
            balance_source=balance_source,
            strategy_state=strategy_state,
        )

        self.logger.info(
            f"Recovery complete: initial_purchase={result.initial_purchase_done}, "
            f"grid_init={result.grid_orders_initialized}, "
            f"reconciled={result.orders_reconciled}, "
            f"orphans={result.orphan_orders_found}, "
            f"ghosts={result.ghost_orders_found}, "
            f"filled_while_down={len(result.orders_filled_while_down)}, "
            f"to_replace={len(result.ended_while_down)}, "
            f"balance_source={result.balance_source}"
        )

        # Send recovery notification
        await self._send_recovery_notification(result)

        return result

    def _parse_strategy_state(self, raw_strategy_state: str | None) -> dict | None:
        """Parses the persisted strategy_state JSON blob, returning None if absent or malformed."""
        if not raw_strategy_state:
            return None
        try:
            return json.loads(raw_strategy_state)
        except (json.JSONDecodeError, TypeError) as e:
            self.logger.warning(f"Failed to parse persisted strategy_state, ignoring it: {e}")
            return None

    # ── Step 2: Grid Level Restoration ───────────────────────────────────

    def _restore_grid_levels(self) -> None:
        saved_levels = self.repository.load_grid_levels()
        if not saved_levels:
            self.logger.info("No saved grid levels to restore.")
            return

        saved_by_price = {row["price"]: row for row in saved_levels}

        # Pass 1: Restore states
        for price, grid_level in self.grid_manager.grid_levels.items():
            saved = saved_by_price.get(price)
            if saved:
                grid_level.state = GridCycleState(saved["state"])

        # Pass 2: Restore paired level references
        for price, grid_level in self.grid_manager.grid_levels.items():
            saved = saved_by_price.get(price)
            if not saved:
                continue
            if saved.get("paired_buy_level_price") is not None:
                paired = self.grid_manager.grid_levels.get(saved["paired_buy_level_price"])
                if paired:
                    grid_level.paired_buy_level = paired
            if saved.get("paired_sell_level_price") is not None:
                paired = self.grid_manager.grid_levels.get(saved["paired_sell_level_price"])
                if paired:
                    grid_level.paired_sell_level = paired

        self.logger.info(f"Restored {len(saved_levels)} grid levels.")

    # ── Step 3: Order Restoration ────────────────────────────────────────

    def _restore_orders(self) -> None:
        saved_orders = self.repository.load_all_orders()
        if not saved_orders:
            self.logger.info("No saved orders to restore.")
            return

        for row in saved_orders:
            order = dict_to_order(row)
            grid_level_price = row.get("grid_level_price")
            is_non_grid = bool(row.get("is_non_grid_order", 0))

            grid_level = None
            if not is_non_grid and grid_level_price is not None:
                grid_level = self.grid_manager.grid_levels.get(grid_level_price)
                if grid_level:
                    grid_level.add_order(order)

            self.order_book.add_order(order, grid_level)

        self.logger.info(f"Restored {len(saved_orders)} orders.")

    def _restore_journal(self) -> bool:
        """
        Reloads the placements no save resolved; one aimed at a level the grid no longer has is dropped. Returns whether
        a liquidation was in progress: an unsent liquidation leg sold nothing, so it is dropped.
        """
        liquidation_in_progress = False
        rows = self.repository.load_pending_placements()
        for row in rows:
            entry = dict_to_pending_placement(row)
            if entry.purpose is PlacementPurpose.LIQUIDATION:
                liquidation_in_progress = True
                if not entry.sent:
                    self.logger.warning(f"Dropping unsent liquidation leg {entry.client_order_id}.")
                    continue
            if entry.order_type is OrderType.LIMIT:
                grid_level = (
                    self.grid_manager.grid_levels.get(entry.level_price) if entry.level_price is not None else None
                )
                if grid_level is None:
                    self.logger.warning(
                        f"Dropping pending {entry.side.value} placement {entry.client_order_id}: the grid has no "
                        f"level at {entry.level_price}."
                    )
                    continue
                if entry.source_level_price not in self.grid_manager.grid_levels:
                    entry.source_level_price = None
                self.grid_manager.mark_level_pending(grid_level, entry.side)
            self.journal.add(entry)
        if rows:
            self.logger.info(f"Restored {len(self.journal)} pending placement(s).")
        return liquidation_in_progress

    # ── Step 4: Exchange Reconciliation ──────────────────────────────────

    async def _reconcile_with_exchange(self, config_hash: str) -> _Reconciliation:
        reconciliation = _Reconciliation(
            followed={entry.source_order_id for entry in self.journal.entries() if entry.source_order_id is not None}
        )
        open_orders = self.order_book.get_open_orders()

        if not open_orders and not len(self.journal):
            self.logger.info("No open orders to reconcile.")
            await self._check_orphans(config_hash, reconciliation)
            return reconciliation

        for order in open_orders:
            try:
                exchange_order = await self.order_execution_strategy.get_order(order.identifier, self.trading_pair)
            except OrderNotFoundError:
                exchange_order = None

            if exchange_order is None:
                self.logger.warning(f"Ghost order {order.identifier} not found on exchange. Marking as canceled.")
                grid_level = self.order_book.get_grid_level_for_order(order)
                order.status = OrderStatus.CANCELED
                self.order_book.remove_open_order(order)
                self._release_grid_level(order)
                reconciliation.executions.append((order, 0.0))
                reconciliation.ghosts += 1
                reconciliation.reconciled += 1
                level_text = f" at level {grid_level.price}" if grid_level is not None else ""
                reconciliation.not_replaced.append(
                    f"ghost {order.identifier} ({order.side.value}{level_text}, fate unknown)"
                )
                continue

            self._apply_exchange_view(order, exchange_order, reconciliation)

        for entry in self.journal.entries():
            if not entry.sent:
                continue
            exchange_order = await self.order_execution_strategy.find_order(entry.client_order_id, self.trading_pair)
            if entry.order_type is OrderType.MARKET:
                self._resolve_market_entry(entry, exchange_order, reconciliation)
            else:
                self._resolve_limit_entry(entry, exchange_order, reconciliation)

        await self._check_orphans(config_hash, reconciliation)
        return reconciliation

    def _note_ended(self, order: Order, reconciliation: _Reconciliation) -> None:
        """
        Queues an order cancelled or expired while down for its re-placement, unless a restored placement already
        re-places it; a rejected one is only reported.
        """
        if order.identifier in reconciliation.followed:
            self.logger.info(f"Order {order.identifier} is already re-placed by a pending placement.")
            return
        grid_level = self.order_book.get_grid_level_for_order(order)
        if order.status is OrderStatus.REJECTED or grid_level is None:
            level_text = f" at level {grid_level.price}" if grid_level is not None else ""
            reconciliation.not_replaced.append(
                f"{order.status.value} {order.identifier} ({order.side.value}{level_text})"
            )
            return
        reconciliation.ended_while_down.append((order, grid_level))

    def _apply_exchange_view(self, order: Order, view: Order, reconciliation: _Reconciliation) -> None:
        """
        Takes the exchange's view of an order the book holds, a saved one or an adopted placement: books what it
        filled while the bot was down, completes the level of a filled order and frees the level of an ended one.
        """
        newly_filled = order.apply_update(view)
        if view.status == OrderStatus.CLOSED:
            self.logger.info(f"Order {order.identifier} filled on exchange while bot was down.")
            self.order_book.remove_open_order(order)
            grid_level = self.order_book.get_grid_level_for_order(order)
            if grid_level:
                self.grid_manager.complete_order(grid_level, order.side, self._level_busy(grid_level))
                if order.identifier in reconciliation.followed:
                    self.logger.info(f"Order {order.identifier} is already paired by a pending placement.")
                else:
                    reconciliation.filled_while_down.append((order, grid_level))
            reconciliation.executions.append((order, newly_filled))
            reconciliation.reconciled += 1
        elif view.status in ENDED_UNFILLED_STATUSES:
            self.logger.info(f"Order {order.identifier} ended as {view.status.value} while bot was down.")
            self.order_book.remove_open_order(order)
            self._release_grid_level(order)
            self._note_ended(order, reconciliation)
            reconciliation.executions.append((order, newly_filled))
            reconciliation.reconciled += 1
        elif newly_filled:
            self.logger.info(f"Order {order.identifier} partly filled while bot was down: {order.filled}.")
            reconciliation.executions.append((order, newly_filled))

    def _resolve_limit_entry(
        self, entry: PendingPlacement, exchange_order: Order | None, reconciliation: _Reconciliation
    ) -> None:
        """Adopts a sent placement the exchange knows, then settles it like a saved order; resends one it does not."""
        if exchange_order is None:
            entry.sent = False
            self.logger.info(f"Pending placement {entry.client_order_id} never reached the exchange; it is sent again.")
            return
        view = copy.copy(exchange_order)
        adopt_order(entry, exchange_order, self.grid_manager, self.order_book)
        self.journal.remove(entry.client_order_id)
        self.logger.info(f"Pending placement {entry.client_order_id} found on the exchange; adopted.")
        if view.status == OrderStatus.OPEN:
            reconciliation.reconciled += 1
        self._apply_exchange_view(exchange_order, view, reconciliation)

    def _resolve_market_entry(
        self, entry: PendingPlacement, exchange_order: Order | None, reconciliation: _Reconciliation
    ) -> None:
        """
        Books, once, a market placement the exchange executed; one it never received or that ended without a fill is
        dropped. Raises OrderOutcomeUnknownError while the exchange has not settled the order.
        """
        if exchange_order is not None and (
            exchange_order.status is OrderStatus.OPEN
            or (not exchange_order.filled and exchange_order.status not in FINAL_STATUSES)
        ):
            raise OrderOutcomeUnknownError(
                f"Pending {entry.purpose.value} {entry.client_order_id} is {exchange_order.status.value} on the "
                "exchange; its outcome is not final yet."
            )
        self.journal.remove(entry.client_order_id)
        if exchange_order is None or not exchange_order.filled:
            self.logger.info(f"Pending {entry.purpose.value} {entry.client_order_id} executed nothing; dropped.")
            return
        self.logger.info(f"Pending {entry.purpose.value} {entry.client_order_id} executed while bot was down.")
        # What a market order did not fill was cancelled, so what it filled is final.
        exchange_order.status = OrderStatus.CLOSED
        exchange_order.average = exchange_order.average or exchange_order.price or entry.price
        self.order_book.add_order(exchange_order)
        reconciliation.executions.append((exchange_order, exchange_order.filled))
        reconciliation.reconciled += 1
        if entry.purpose is PlacementPurpose.INITIAL_PURCHASE:
            reconciliation.initial_purchase_found = True

    async def _check_orphans(self, config_hash: str, reconciliation: _Reconciliation) -> None:
        """Counts the open exchange orders no saved state covers, and alerts about those this bot placed."""
        try:
            exchange_open_orders = await self.exchange_service.fetch_open_orders(self.trading_pair)
        except Exception as e:
            self.logger.warning(f"Failed to fetch exchange open orders for orphan check: {e}")
            return
        own_prefix = client_order_id_prefix(config_hash)
        local_ids = {o.identifier for o in self.order_book.get_open_orders()}
        journal_ids = {entry.client_order_id for entry in self.journal.entries()}
        own = []
        for remote in exchange_open_orders:
            if remote.get("id") in local_ids or remote.get("clientOrderId") in journal_ids:
                continue
            reconciliation.orphans += 1
            if str(remote.get("clientOrderId") or "").startswith(own_prefix):
                own.append(remote)
                self.logger.warning(
                    f"Orphan of this bot on exchange: {remote.get('id')} "
                    f"(side={remote.get('side')}, price={remote.get('price')}). Not auto-adopting."
                )
            else:
                self.logger.warning(
                    f"Orphan order on exchange: {remote.get('id')} "
                    f"(side={remote.get('side')}, price={remote.get('price')}). "
                    f"Not auto-adopting."
                )
        if not own:
            return
        listed = ", ".join(f"{o.get('id')} ({o.get('side')} at {o.get('price')})" for o in own)
        try:
            await self.notification_handler.async_send_notification(
                NotificationType.ERROR_OCCURRED,
                error_details=(
                    f"{len(own)} order(s) placed by this bot are open on the exchange but in no saved state: "
                    f"{listed}. They are not adopted: cancel them on the exchange, or leave them if you meant to."
                ),
            )
        except Exception as e:
            self.logger.warning(f"Failed to send the alert about this bot's orphan orders ({listed}): {e}")

    # ── Step 5: Balance Restoration ──────────────────────────────────────

    def _level_busy(self, grid_level: GridLevel) -> bool:
        return self.order_book.has_open_orders_at(grid_level) or bool(self.journal.pending_at(grid_level.price))

    def _release_grid_level(self, order: Order) -> None:
        """Frees the grid level of a cancelled order unless another open order or pending placement still sits on it."""
        grid_level = self.order_book.get_grid_level_for_order(order)
        if grid_level:
            self.grid_manager.rollback_order_placement(grid_level, order.side, self._level_busy(grid_level))

    @staticmethod
    def _reservations(held: list[tuple[OrderSide, float, float]]) -> tuple[Decimal, Decimal]:
        """
        What `held` ((side, quantity, price)) needs in reserve: (fiat, crypto). Each amount is taken at the tracker's
        precision, the way the release that later frees it takes it.
        """
        fiat = sum(
            (_quantized(Decimal(str(quantity * price))) for side, quantity, price in held if side == OrderSide.BUY),
            Decimal("0"),
        )
        crypto = sum(
            (_quantized(Decimal(str(quantity))) for side, quantity, _ in held if side == OrderSide.SELL),
            Decimal("0"),
        )
        return fiat, crypto

    def _entry_reservations(self) -> tuple[Decimal, Decimal]:
        """What the pending limit placements hold in reserve: (fiat, crypto)."""
        return self._reservations([(e.side, e.quantity, e.limit_price) for e in self.journal.limit_entries()])

    def _needed_reservations(self) -> tuple[Decimal, Decimal]:
        """What the open orders and the pending limit placements need in reserve: (fiat, crypto)."""
        held = [(o.side, o.remaining, o.price) for o in self.order_book.get_open_orders()]
        held += [(e.side, e.quantity, e.limit_price) for e in self.journal.limit_entries()]
        return self._reservations(held)

    def _resplit_reservations(self) -> None:
        """Reserves exactly what the open orders and the pending placements need, keeping the restored totals."""
        tracker = self.balance_tracker
        fiat_total = tracker._balance + tracker._reserved_fiat
        crypto_total = tracker._crypto_balance + tracker._reserved_crypto
        tracker._reserved_fiat, tracker._reserved_crypto = self._needed_reservations()
        tracker._balance = fiat_total - tracker._reserved_fiat
        tracker._crypto_balance = crypto_total - tracker._reserved_crypto

    async def _restore_balance(self, executions: list[tuple[Order, float]]) -> str:
        if self.config_manager.get_trading_mode() == TradingMode.PAPER_TRADING:
            await self._restore_balance_from_db(executions)
            return "db"
        await self._restore_balance_from_exchange()
        return "exchange"

    async def _restore_balance_from_db(self, executions: list[tuple[Order, float]]) -> None:
        """
        Paper trading keeps a virtual balance the testnet account does not reflect: it resumes from the database,
        then books every execution seen at restart like a live fill.
        """
        saved_balance = self.repository.load_balance_state()
        if saved_balance is None:
            raise ValueError("The database holds a bot state without its balance state.")
        tracker = self.balance_tracker
        tracker._balance = Decimal(saved_balance["fiat_balance"])
        tracker._crypto_balance = Decimal(saved_balance["crypto_balance"])
        tracker._reserved_fiat = Decimal(saved_balance["reserved_fiat"])
        tracker._reserved_crypto = Decimal(saved_balance["reserved_crypto"])
        tracker._total_fees = Decimal(saved_balance["total_fees"])
        try:
            self._restore_saved_accounting(saved_balance)
        except Exception as e:
            self.logger.error(f"Failed to restore cost basis and PnL, starting them over: {e}", exc_info=True)
        # A market order reserved nothing, so it is booked like the purchase or the liquidation it was part of.
        for order, quantity in executions:
            if order.order_type is not OrderType.MARKET:
                await tracker.record_execution(order, quantity)
            elif order.side is OrderSide.BUY:
                await tracker.update_after_initial_purchase(order)
            else:
                await tracker.update_after_liquidation(order)
        # A placement written after the last full save is in the journal but not in the saved reservations.
        self._resplit_reservations()
        self.logger.info(f"Balance restored from the database with {len(executions)} execution(s) booked.")

    async def _restore_balance_from_exchange(self) -> None:
        saved_balance = self.repository.load_balance_state()

        exchange_balance = await self.exchange_service.get_balance()
        base = self.config_manager.get_base_currency()
        quote = self.config_manager.get_quote_currency()
        exchange_fiat = Decimal(str(exchange_balance.get("free", {}).get(quote, 0)))
        exchange_crypto = Decimal(str(exchange_balance.get("free", {}).get(base, 0)))

        # Exchange "free" excludes the funds of its open orders but still holds those of the pending placements,
        # which the bot reserves.
        reserved_fiat, reserved_crypto = self._needed_reservations()
        entry_fiat, entry_crypto = self._entry_reservations()
        self.balance_tracker._balance = exchange_fiat - entry_fiat
        self.balance_tracker._crypto_balance = exchange_crypto - entry_crypto
        self.balance_tracker._reserved_fiat = reserved_fiat
        self.balance_tracker._reserved_crypto = reserved_crypto

        # Total fees from DB (exchange doesn't track cumulative fees)
        if saved_balance:
            self.balance_tracker._total_fees = Decimal(saved_balance["total_fees"])

        self.logger.info(
            f"Balance restored from exchange: "
            f"fiat={self.balance_tracker.balance}, "
            f"crypto={self.balance_tracker.crypto_balance}, "
            f"reserved_fiat={self.balance_tracker.reserved_fiat}, "
            f"reserved_crypto={self.balance_tracker.reserved_crypto}"
        )

    def _restore_saved_accounting(self, saved_balance: SavedBalance) -> None:
        self.balance_tracker.restore_accounting(
            avg_cost=_optional_decimal(saved_balance.get("avg_cost")),
            realized_pnl=Decimal(saved_balance.get("realized_pnl") or "0"),
            peak_value=_optional_decimal(saved_balance.get("peak_value")),
        )

    def _restore_accounting(self, executions: list[tuple[Order, float]]) -> None:
        saved_balance = self.repository.load_balance_state()
        if not saved_balance:
            return

        self._restore_saved_accounting(saved_balance)
        if executions:
            starting_position = Decimal(saved_balance["crypto_balance"]) + Decimal(saved_balance["reserved_crypto"])
            self.balance_tracker.replay_fills_on_cost_basis(starting_position, executions)

    # ── Notification ─────────────────────────────────────────────────────

    async def _send_recovery_notification(self, result: RecoveryResult) -> None:
        details = (
            f"Orders reconciled: {result.orders_reconciled}\n"
            f"Orders filled while down: {len(result.orders_filled_while_down)}\n"
            f"Orders re-placed after the restart: {len(result.ended_while_down)}\n"
            f"Ghost orders: {result.ghost_orders_found}\n"
            f"Orphan orders: {result.orphan_orders_found}\n"
            f"Balance source: {result.balance_source}"
        )
        if result.not_replaced:
            details += "\nNot re-placed: " + "; ".join(result.not_replaced)
        await self.notification_handler.async_send_notification(
            NotificationType.STATE_RECOVERY_COMPLETE,
            recovery_details=details,
        )
