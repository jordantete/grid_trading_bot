from collections.abc import Awaitable, Callable
import copy
from dataclasses import dataclass, field
import logging

from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.grid_management.grid_level import GridLevel
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.order_handling.execution_strategy.order_execution_strategy_interface import (
    OrderExecutionStrategyInterface,
)
from grid_trading_bot.core.order_handling.order import (
    ENDED_UNFILLED_STATUSES,
    FINAL_STATUSES,
    Order,
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
from grid_trading_bot.core.services.exceptions import OrderNotFoundError, OrderOutcomeUnknownError
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface


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


class ExchangeResolution:
    """Settles the restored orders and placements against the exchange, and counts the orders no saved state covers."""

    def __init__(
        self,
        grid_manager: GridManager,
        order_book: OrderBook,
        journal: PlacementJournal,
        exchange_service: ExchangeInterface,
        order_execution_strategy: OrderExecutionStrategyInterface,
        trading_pair: str,
        notify: Callable[..., Awaitable[None]],
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.grid_manager = grid_manager
        self.order_book = order_book
        self.journal = journal
        self.exchange_service = exchange_service
        self.order_execution_strategy = order_execution_strategy
        self.trading_pair = trading_pair
        self._notify = notify

    async def reconcile(self, config_hash: str) -> _Reconciliation:
        """
        Takes the exchange's view of every open order and sent placement restored, then checks for orphans: updates
        statuses, completes the levels of filled orders and resolves the placements already sent.
        """
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
            await self._notify(
                NotificationType.ERROR_OCCURRED,
                error_details=(
                    f"{len(own)} order(s) placed by this bot are open on the exchange but in no saved state: "
                    f"{listed}. They are not adopted: cancel them on the exchange, or leave them if you meant to."
                ),
            )
        except Exception as e:
            self.logger.warning(f"Failed to send the alert about this bot's orphan orders ({listed}): {e}")

    def _level_busy(self, grid_level: GridLevel) -> bool:
        return self.order_book.has_open_orders_at(grid_level) or bool(self.journal.pending_at(grid_level.price))

    def _release_grid_level(self, order: Order) -> None:
        """Frees the grid level of a cancelled order unless another open order or pending placement still sits on it."""
        grid_level = self.order_book.get_grid_level_for_order(order)
        if grid_level:
            self.grid_manager.rollback_order_placement(grid_level, order.side, self._level_busy(grid_level))
