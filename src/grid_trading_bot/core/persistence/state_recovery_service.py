from dataclasses import dataclass, field
from decimal import Decimal
import logging

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.grid_management.grid_level import GridCycleState
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.execution_strategy.order_execution_strategy_interface import (
    OrderExecutionStrategyInterface,
)
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface

from .serializers import compute_config_hash, dict_to_order
from .state_repository_interface import StateRepositoryInterface


@dataclass
class RecoveryResult:
    recovered: bool
    initial_purchase_done: bool = False
    grid_orders_initialized: bool = False
    orders_reconciled: int = 0
    orphan_orders_found: int = 0
    ghost_orders_found: int = 0
    balance_source: str = "db"
    errors: list[str] = field(default_factory=list)


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

    async def attempt_recovery(self) -> RecoveryResult:
        try:
            return await self._do_recovery()
        except Exception as e:
            self.logger.error(f"Recovery failed with unexpected error: {e}", exc_info=True)
            self.logger.warning("Falling back to fresh start.")
            self.repository.clear_all()
            return RecoveryResult(recovered=False, errors=[str(e)])

    async def _do_recovery(self) -> RecoveryResult:
        bot_state = self.repository.load_bot_state()
        if bot_state is None:
            self.logger.info("No previous state found. Starting fresh.")
            return RecoveryResult(recovered=False)

        # Step 1: Config compatibility
        current_hash = compute_config_hash(self.config_manager)
        if bot_state["config_hash"] != current_hash:
            self.logger.warning("Config hash mismatch — grid configuration changed since last run. Starting fresh.")
            self.repository.clear_all()
            return RecoveryResult(recovered=False)

        self.logger.info("Previous state found. Attempting recovery...")

        # Step 2: Restore grid levels
        self._restore_grid_levels()

        # Step 3: Restore orders + OrderBook
        self._restore_orders()

        # Step 4: Merge with exchange
        reconcile_stats = await self._reconcile_with_exchange()

        # Step 5: Restore balance
        balance_source = await self._restore_balance()

        result = RecoveryResult(
            recovered=True,
            initial_purchase_done=bot_state["initial_purchase_done"],
            grid_orders_initialized=bot_state["grid_orders_initialized"],
            orders_reconciled=reconcile_stats["reconciled"],
            orphan_orders_found=reconcile_stats["orphans"],
            ghost_orders_found=reconcile_stats["ghosts"],
            balance_source=balance_source,
        )

        self.logger.info(
            f"Recovery complete: initial_purchase={result.initial_purchase_done}, "
            f"grid_init={result.grid_orders_initialized}, "
            f"reconciled={result.orders_reconciled}, "
            f"orphans={result.orphan_orders_found}, "
            f"ghosts={result.ghost_orders_found}, "
            f"balance_source={result.balance_source}"
        )
        return result

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

    # ── Step 4: Exchange Reconciliation ──────────────────────────────────

    async def _reconcile_with_exchange(self) -> dict[str, int]:
        stats = {"reconciled": 0, "orphans": 0, "ghosts": 0}
        open_orders = self.order_book.get_open_orders()

        if not open_orders:
            self.logger.info("No open orders to reconcile.")
            return stats

        # Check each locally open order against exchange
        for order in open_orders:
            try:
                exchange_order = await self.order_execution_strategy.get_order(order.identifier, self.trading_pair)
            except Exception as e:
                self.logger.warning(f"Failed to fetch order {order.identifier} from exchange: {e}")
                continue

            if exchange_order is None:
                # Order not found on exchange → mark as canceled
                self.logger.warning(f"Ghost order {order.identifier} not found on exchange. Marking as canceled.")
                self.order_book.update_order_status(order.identifier, OrderStatus.CANCELED)
                await self._release_reserved_for_order(order)
                stats["ghosts"] += 1
                stats["reconciled"] += 1

            elif exchange_order.status == OrderStatus.CLOSED:
                self.logger.info(f"Order {order.identifier} filled on exchange while bot was down.")
                order.status = OrderStatus.CLOSED
                order.filled = exchange_order.filled
                order.remaining = exchange_order.remaining
                order.average = exchange_order.average
                order.cost = exchange_order.cost
                self.order_book.remove_open_order(order)
                stats["reconciled"] += 1

            elif exchange_order.status == OrderStatus.CANCELED:
                self.logger.info(f"Order {order.identifier} was canceled on exchange while bot was down.")
                self.order_book.update_order_status(order.identifier, OrderStatus.CANCELED)
                await self._release_reserved_for_order(order)
                stats["reconciled"] += 1

            # OPEN on exchange → keep as-is

        # Check for orphan orders on exchange not in our book
        try:
            exchange_open_orders = await self.exchange_service.fetch_open_orders(self.trading_pair)
            local_ids = {o.identifier for o in self.order_book.get_open_orders()}
            for remote in exchange_open_orders:
                if remote["id"] not in local_ids:
                    stats["orphans"] += 1
                    self.logger.warning(
                        f"Orphan order on exchange: {remote['id']} "
                        f"(side={remote.get('side')}, price={remote.get('price')}). "
                        f"Not auto-adopting."
                    )
        except Exception as e:
            self.logger.warning(f"Failed to fetch exchange open orders for orphan check: {e}")

        return stats

    async def _release_reserved_for_order(self, order: Order) -> None:
        if order.side == OrderSide.BUY:
            reserved_amount = order.price * order.remaining
            await self.balance_tracker.release_reserved_fiat(reserved_amount)
        else:
            await self.balance_tracker.release_reserved_crypto(order.remaining)

    # ── Step 5: Balance Restoration ──────────────────────────────────────

    async def _restore_balance(self) -> str:
        saved_balance = self.repository.load_balance_state()

        try:
            exchange_balance = await self.exchange_service.get_balance()
            base = self.config_manager.get_base_currency()
            quote = self.config_manager.get_quote_currency()
            exchange_fiat = Decimal(str(exchange_balance.get("free", {}).get(quote, 0)))
            exchange_crypto = Decimal(str(exchange_balance.get("free", {}).get(base, 0)))

            if saved_balance:
                reserved_fiat = Decimal(saved_balance["reserved_fiat"])
                reserved_crypto = Decimal(saved_balance["reserved_crypto"])
                total_fees = Decimal(saved_balance["total_fees"])

                # Cap reserved at exchange free to prevent negative available
                reserved_fiat = min(reserved_fiat, exchange_fiat)
                reserved_crypto = min(reserved_crypto, exchange_crypto)

                self.balance_tracker._balance = exchange_fiat - reserved_fiat
                self.balance_tracker._crypto_balance = exchange_crypto - reserved_crypto
                self.balance_tracker._reserved_fiat = reserved_fiat
                self.balance_tracker._reserved_crypto = reserved_crypto
                self.balance_tracker._total_fees = total_fees
            else:
                self.balance_tracker._balance = exchange_fiat
                self.balance_tracker._crypto_balance = exchange_crypto

            self.logger.info(
                f"Balance restored from exchange: "
                f"fiat={self.balance_tracker.balance}, "
                f"crypto={self.balance_tracker.crypto_balance}, "
                f"reserved_fiat={self.balance_tracker.reserved_fiat}, "
                f"reserved_crypto={self.balance_tracker.reserved_crypto}"
            )
            return "exchange"

        except Exception as e:
            self.logger.warning(f"Failed to fetch exchange balance: {e}. Falling back to DB.")
            if saved_balance:
                self.balance_tracker._balance = Decimal(saved_balance["fiat_balance"])
                self.balance_tracker._crypto_balance = Decimal(saved_balance["crypto_balance"])
                self.balance_tracker._total_fees = Decimal(saved_balance["total_fees"])
                self.balance_tracker._reserved_fiat = Decimal(saved_balance["reserved_fiat"])
                self.balance_tracker._reserved_crypto = Decimal(saved_balance["reserved_crypto"])
                return "db"

            self.logger.error("No saved balance and exchange unavailable. Starting with zero balances.")
            return "none"
