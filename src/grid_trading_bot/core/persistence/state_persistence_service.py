import asyncio
from collections.abc import Awaitable, Callable
import contextlib
import inspect
import json
import logging
from typing import Any

from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest
from grid_trading_bot.core.bot_management.supervised_loop import LoopState, SupervisedLoop
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.core.order_handling.placement_journal import PendingPlacement, PlacementJournal
from grid_trading_bot.core.order_handling.state_store import ArmingFlags

from .serializers import (
    balance_to_dict,
    compute_config_hash,
    grid_level_to_dict,
    order_to_dict,
    pending_placement_to_dict,
)
from .state_repository_interface import StateRepositoryInterface, StateSnapshot


class StatePersistenceService:
    MAX_CONSECUTIVE_FAILURES = 3
    ALERT_DRAIN_TIMEOUT = 5.0

    def __init__(
        self,
        repository: StateRepositoryInterface,
        event_bus: EventBus,
        notification_handler: NotificationHandler,
        order_book: OrderBook,
        grid_manager: GridManager,
        balance_tracker: BalanceTracker,
        config_manager: Any,
        trading_pair: str,
        strategy_type: str,
        strategy_state_provider: Callable[[], dict] | None = None,
        journal: PlacementJournal | None = None,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.repository = repository
        self.event_bus = event_bus
        self.notification_handler = notification_handler
        self.order_book = order_book
        self.grid_manager = grid_manager
        self.balance_tracker = balance_tracker
        self.config_manager = config_manager
        self.trading_pair = trading_pair
        self.strategy_type = strategy_type
        self.strategy_state_provider = strategy_state_provider
        self.journal = journal
        self._config_hash = compute_config_hash(config_manager)
        self._consecutive_failures = 0
        self._halt_task: asyncio.Task | None = None
        self._periodic_task: asyncio.Task | None = None
        self._checkpoint_loop: SupervisedLoop | None = None
        self._alert_tasks: set[asyncio.Task] = set()
        self._last_alert: asyncio.Task | None = None

    def snapshot(self, arming: ArmingFlags) -> StateSnapshot:
        """The state of every component at this instant, as the repository stores it."""
        orders = [
            order_to_dict(order, grid_level.price if grid_level else None, grid_level is None)
            for order, grid_level in (
                self.order_book.get_buy_orders_with_grid() + self.order_book.get_sell_orders_with_grid()
            )
        ]
        strategy_state = self.strategy_state_provider() if self.strategy_state_provider else None
        return StateSnapshot(
            bot_state={
                "config_hash": self._config_hash,
                "trading_pair": self.trading_pair,
                "strategy_type": self.strategy_type,
                "initial_purchase_done": arming.initial_purchase_done,
                "grid_orders_initialized": arming.grid_orders_initialized,
                "strategy_state": json.dumps(strategy_state) if strategy_state is not None else None,
            },
            balance_state=balance_to_dict(self.balance_tracker),
            orders=orders,
            grid_levels=[grid_level_to_dict(level) for level in self.grid_manager.grid_levels.values()],
            pending_placements=(
                [pending_placement_to_dict(entry) for entry in self.journal.entries()]
                if self.journal is not None
                else []
            ),
        )

    async def save(self, arming: ArmingFlags) -> None:
        """Writes the current state in one transaction. A failure is alerted and counted, never raised."""
        try:
            snapshot = self.snapshot(arming)
            await asyncio.to_thread(self.repository.save_state, snapshot)
        except Exception as error:
            self._record_failure(error)
            return
        self._record_success()

    async def save_pending(self, entry: PendingPlacement) -> bool:
        """
        Writes one journal entry before its order is sent, so a restart finds the order by its client id. A failure
        is alerted and counted like a failed checkpoint, never raised.
        """
        try:
            await asyncio.to_thread(self.repository.save_pending, pending_placement_to_dict(entry))
        except Exception as error:
            self._record_failure(error)
            return False
        self._record_success()
        return True

    def _record_failure(self, error: Exception) -> None:
        self._consecutive_failures += 1
        self.logger.error(f"Checkpoint failed ({self._consecutive_failures} in a row): {error}", exc_info=error)
        if self._consecutive_failures == 1:
            self._alert(NotificationType.CHECKPOINT_FAILED, error_details=str(error))
        if self._consecutive_failures == self.MAX_CONSECUTIVE_FAILURES:
            request = StopRequest(
                StopKind.PERSISTENCE,
                f"{self._consecutive_failures} checkpoints failed in a row; last error: {error}",
            )
            # Published from its own task: the failed save runs under the OrderManager lock, which the halt needs.
            self._halt_task = asyncio.create_task(self.event_bus.publish(Events.STOP_BOT, request))

    def _record_success(self) -> None:
        if self._consecutive_failures:
            failures, self._consecutive_failures = self._consecutive_failures, 0
            self._alert(NotificationType.CHECKPOINT_RESTORED, failures=failures)
        self.logger.debug("Checkpoint written.")

    def _alert(self, notification_type: NotificationType, **details: Any) -> None:
        # Sent from a task so a slow notification never holds the save, which runs under the OrderManager lock.
        task = asyncio.create_task(self._send_alert(self._last_alert, notification_type, details))
        self._alert_tasks.add(task)
        task.add_done_callback(self._alert_tasks.discard)
        self._last_alert = task

    async def _send_alert(
        self, previous: asyncio.Task | None, notification_type: NotificationType, details: dict[str, Any]
    ) -> None:
        if previous is not None:
            await asyncio.wait({previous})
        try:
            await self.notification_handler.async_send_notification(notification_type, **details)
        except Exception as error:
            self.logger.error(f"Checkpoint alert {notification_type.name} could not be sent: {error}", exc_info=True)

    async def wait_for_alerts(self, timeout: float | None = None) -> None:
        """Waits until the alerts raised so far are sent, cancelling those still pending after `timeout` seconds."""
        if not self._alert_tasks:
            return
        _, pending = await asyncio.wait(set(self._alert_tasks), timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            self.logger.warning(f"{len(pending)} checkpoint alert(s) still unsent after {timeout} s; dropped.")
            await asyncio.wait(pending)

    def start_periodic_checkpoints(self, interval_seconds: float, checkpoint: Callable[[], Awaitable[None]]) -> None:
        """Calls `checkpoint` on every interval, so state that changes between order operations (the trailing-stop
        ratchet) is saved too. A failed write is counted by `save`; an exception that escapes `checkpoint` is handled
        by the supervised loop."""
        if self._periodic_task and not self._periodic_task.done():
            return
        self._checkpoint_loop = SupervisedLoop(
            "Periodic checkpoint",
            interval_seconds,
            checkpoint,
            run_first=False,
            event_bus=self.event_bus,
            notify=self.notification_handler.async_send_notification,
        )
        self._periodic_task = asyncio.create_task(self._periodic_checkpoint_loop(self._checkpoint_loop))

    async def _periodic_checkpoint_loop(self, loop: SupervisedLoop) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await loop.run()

    @property
    def checkpoint_loop_state(self) -> LoopState | None:
        """The periodic checkpoint loop's state, None before it first started."""
        return self._checkpoint_loop.state if self._checkpoint_loop is not None else None

    async def cleanup(self) -> None:
        if self._periodic_task:
            self._periodic_task.cancel()
            self._periodic_task = None
        # A halt task that already started is the one running the shutdown that calls this cleanup.
        halt_coro = self._halt_task.get_coro() if self._halt_task else None
        if (
            self._halt_task is not None
            and halt_coro is not None
            and inspect.getcoroutinestate(halt_coro) == inspect.CORO_CREATED
        ):
            self._halt_task.cancel()
        await self.wait_for_alerts(self.ALERT_DRAIN_TIMEOUT)
        self.repository.close()
