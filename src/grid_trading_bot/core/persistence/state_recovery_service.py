from dataclasses import dataclass, field
from enum import Enum
import json
import logging

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.grid_management.grid_level import GridCycleState, GridLevel
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.order_handling.balance_source import BalanceSource
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.execution_strategy.order_execution_strategy_interface import (
    OrderExecutionStrategyInterface,
)
from grid_trading_bot.core.order_handling.order import Order, OrderType
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.core.order_handling.placement_journal import PlacementJournal, PlacementPurpose
from grid_trading_bot.core.services.exceptions import DataFetchError
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface

from .balance_restore import BalanceRestore
from .exchange_resolution import ExchangeResolution
from .serializers import compute_config_hash, dict_to_order, dict_to_pending_placement
from .state_repository_interface import StateRepositoryInterface


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
        balance_source: BalanceSource,
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
        self.balance_source = balance_source
        self.journal = journal if journal is not None else PlacementJournal()
        self.exchange_resolution = ExchangeResolution(
            grid_manager=grid_manager,
            order_book=order_book,
            journal=self.journal,
            exchange_service=exchange_service,
            order_execution_strategy=order_execution_strategy,
            trading_pair=trading_pair,
            notify=notification_handler.async_send_notification,
        )
        self.balance_restore = BalanceRestore(
            repository=repository,
            order_book=order_book,
            balance_tracker=balance_tracker,
            exchange_service=exchange_service,
            journal=self.journal,
            base_currency=config_manager.config.pair.base_currency,
            quote_currency=config_manager.config.pair.quote_currency,
            notify=notification_handler.async_send_notification,
        )

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
        reconciliation = await self.exchange_resolution.reconcile(current_hash)

        # Step 5: Restore balance (reserved recalculated from the open orders and the pending placements)
        balance_source = await self._restore_balance(reconciliation.executions)

        # Step 6: Restore cost basis, realized PnL and peak, then book the fills made while down
        if balance_source == "exchange":
            try:
                self.balance_restore.restore_accounting(reconciliation.executions)
            except Exception as e:
                # Reporting-only state: losing it must not turn a good recovery into a fresh start.
                self.logger.error(
                    f"Failed to restore or replay the cost basis and PnL; they may be incomplete: {e}", exc_info=True
                )
                await self.balance_restore.alert_accounting_not_restored(e)

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

    # ── Step 5: Balance Restoration ──────────────────────────────────────

    async def _restore_balance(self, executions: list[tuple[Order, float]]) -> str:
        if self.balance_source is BalanceSource.VIRTUAL:
            await self.balance_restore.restore_from_database(executions)
            return "db"
        await self.balance_restore.restore_from_exchange()
        return "exchange"

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
