import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
from typing import Any

from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.core.order_handling.state_store import ArmingFlags

from .serializers import balance_to_dict, compute_config_hash, grid_level_to_dict, order_to_dict
from .state_repository_interface import StateRepositoryInterface, StateSnapshot


class StatePersistenceService:
    MAX_CONSECUTIVE_FAILURES = 3

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
        self._config_hash = compute_config_hash(config_manager)
        self._consecutive_failures = 0
        self._halt_task: asyncio.Task | None = None
        self._periodic_task: asyncio.Task | None = None

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
        )

    async def save(self, arming: ArmingFlags) -> None:
        """Writes the current state in one transaction. A failure is alerted and counted, never raised."""
        try:
            snapshot = self.snapshot(arming)
            await asyncio.to_thread(self.repository.save_state, snapshot)
        except Exception as error:
            await self._record_failure(error)
            return
        await self._record_success()

    async def _record_failure(self, error: Exception) -> None:
        self._consecutive_failures += 1
        self.logger.error(f"Checkpoint failed ({self._consecutive_failures} in a row): {error}", exc_info=error)
        if self._consecutive_failures == 1:
            await self.notification_handler.async_send_notification(
                NotificationType.CHECKPOINT_FAILED, error_details=str(error)
            )
        if self._consecutive_failures == self.MAX_CONSECUTIVE_FAILURES:
            request = StopRequest(
                StopKind.PERSISTENCE,
                f"{self._consecutive_failures} checkpoints failed in a row; last error: {error}",
            )
            # Published from its own task: the failed save runs under the OrderManager lock, which the halt needs.
            self._halt_task = asyncio.create_task(self.event_bus.publish(Events.STOP_BOT, request))

    async def _record_success(self) -> None:
        if self._consecutive_failures:
            failures, self._consecutive_failures = self._consecutive_failures, 0
            await self.notification_handler.async_send_notification(
                NotificationType.CHECKPOINT_RESTORED, failures=failures
            )
        self.logger.debug("Checkpoint written.")

    def start_periodic_checkpoints(self, interval_seconds: float, checkpoint: Callable[[], Awaitable[None]]) -> None:
        """Calls `checkpoint` on every interval, so state that changes between order operations (the trailing-stop
        ratchet) is saved too."""
        if self._periodic_task and not self._periodic_task.done():
            return
        self._periodic_task = asyncio.create_task(self._periodic_checkpoint_loop(interval_seconds, checkpoint))

    async def _periodic_checkpoint_loop(
        self, interval_seconds: float, checkpoint: Callable[[], Awaitable[None]]
    ) -> None:
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                await checkpoint()
        except asyncio.CancelledError:
            pass

    def cleanup(self) -> None:
        if self._periodic_task:
            self._periodic_task.cancel()
            self._periodic_task = None
        self.repository.close()
