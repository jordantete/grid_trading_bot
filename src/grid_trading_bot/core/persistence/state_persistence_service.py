import asyncio
import logging
from typing import Any

from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.order import Order
from grid_trading_bot.core.order_handling.order_book import OrderBook

from .serializers import balance_to_dict, compute_config_hash, grid_level_to_dict, order_to_dict
from .state_repository_interface import StateRepositoryInterface


class StatePersistenceService:
    def __init__(
        self,
        repository: StateRepositoryInterface,
        event_bus: EventBus,
        order_book: OrderBook,
        grid_manager: GridManager,
        balance_tracker: BalanceTracker,
        config_manager: Any,
        trading_pair: str,
        strategy_type: str,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.repository = repository
        self.event_bus = event_bus
        self.order_book = order_book
        self.grid_manager = grid_manager
        self.balance_tracker = balance_tracker
        self.config_manager = config_manager
        self.trading_pair = trading_pair
        self.strategy_type = strategy_type
        self._config_hash = compute_config_hash(config_manager)
        self._initial_purchase_done = False
        self._grid_orders_initialized = False

        self.event_bus.subscribe(Events.ORDER_FILLED, self._on_order_filled)
        self.event_bus.subscribe(Events.ORDER_CANCELLED, self._on_order_cancelled)
        self.event_bus.subscribe(Events.INITIAL_PURCHASE_DONE, self._on_initial_purchase_done)
        self.event_bus.subscribe(Events.GRID_ORDERS_INITIALIZED, self._on_grid_orders_initialized)

    async def _on_order_filled(self, order: Order) -> None:
        await self._checkpoint()

    async def _on_order_cancelled(self, order: Order) -> None:
        await self._checkpoint()

    async def _on_initial_purchase_done(self, data: Any) -> None:
        self._initial_purchase_done = True
        await self._checkpoint()

    async def _on_grid_orders_initialized(self, data: Any) -> None:
        self._grid_orders_initialized = True
        await self._checkpoint()

    async def _checkpoint(self) -> None:
        try:
            await asyncio.to_thread(self._write_checkpoint)
        except Exception as e:
            self.logger.error(f"Failed to write checkpoint: {e}", exc_info=True)

    def _write_checkpoint(self) -> None:
        self.repository.save_bot_state(
            {
                "config_hash": self._config_hash,
                "trading_pair": self.trading_pair,
                "strategy_type": self.strategy_type,
                "initial_purchase_done": self._initial_purchase_done,
                "grid_orders_initialized": self._grid_orders_initialized,
            }
        )

        self.repository.save_balance_state(balance_to_dict(self.balance_tracker))

        order_dicts = []
        for order, grid_level in self.order_book.get_buy_orders_with_grid():
            is_non_grid = grid_level is None
            gl_price = grid_level.price if grid_level else None
            order_dicts.append(order_to_dict(order, gl_price, is_non_grid))
        for order, grid_level in self.order_book.get_sell_orders_with_grid():
            is_non_grid = grid_level is None
            gl_price = grid_level.price if grid_level else None
            order_dicts.append(order_to_dict(order, gl_price, is_non_grid))
        self.repository.save_orders(order_dicts)

        grid_level_dicts = [grid_level_to_dict(gl) for gl in self.grid_manager.grid_levels.values()]
        self.repository.save_grid_levels(grid_level_dicts)

        self.logger.debug("Checkpoint written successfully.")

    def set_flags(self, initial_purchase_done: bool, grid_orders_initialized: bool) -> None:
        self._initial_purchase_done = initial_purchase_done
        self._grid_orders_initialized = grid_orders_initialized

    def cleanup(self) -> None:
        self.event_bus.unsubscribe(Events.ORDER_FILLED, self._on_order_filled)
        self.event_bus.unsubscribe(Events.ORDER_CANCELLED, self._on_order_cancelled)
        self.event_bus.unsubscribe(Events.INITIAL_PURCHASE_DONE, self._on_initial_purchase_done)
        self.event_bus.unsubscribe(Events.GRID_ORDERS_INITIALIZED, self._on_grid_orders_initialized)
        self.repository.close()
