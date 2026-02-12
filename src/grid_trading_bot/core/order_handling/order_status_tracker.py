import asyncio
import logging

from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.order_handling.execution_strategy.order_execution_strategy_interface import (
    OrderExecutionStrategyInterface,
)
from grid_trading_bot.core.order_handling.order import Order, OrderStatus
from grid_trading_bot.core.order_handling.order_book import OrderBook


class OrderStatusTracker:
    """
    Tracks the status of pending orders and publishes events
    when their states change (e.g., open, filled, canceled).
    """

    def __init__(
        self,
        order_book: OrderBook,
        order_execution_strategy: OrderExecutionStrategyInterface,
        event_bus: EventBus,
        polling_interval: float = 15.0,
    ):
        """
        Initializes the OrderStatusTracker.

        Args:
            order_book: OrderBook instance to manage and query orders.
            order_execution_strategy: Strategy for querying order statuses from the exchange.
            event_bus: EventBus instance for publishing state change events.
            polling_interval: Time interval (in seconds) between status checks.
        """
        self.order_book = order_book
        self.order_execution_strategy = order_execution_strategy
        self.event_bus = event_bus
        self.polling_interval = polling_interval
        self._monitoring_task = None
        self._active_tasks = set()
        self.logger = logging.getLogger(self.__class__.__name__)

    async def _track_open_order_statuses(self) -> None:
        """
        Periodically checks the statuses of open orders and updates their states.
        """
        try:
            while True:
                await self._process_open_orders()
                await asyncio.sleep(self.polling_interval)

        except asyncio.CancelledError:
            self.logger.info("OrderStatusTracker monitoring task was cancelled.")
            await self._cancel_active_tasks()

        except Exception as error:
            self.logger.error(f"Unexpected error in OrderStatusTracker: {error}")

    async def _process_open_orders(self) -> None:
        """
        Processes open orders by querying their statuses and handling state changes.
        """
        open_orders = self.order_book.get_open_orders()
        tasks = [self._create_task(self._query_and_handle_order(order)) for order in open_orders]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                self.logger.error(f"Error during order processing: {result}", exc_info=True)

    async def _query_and_handle_order(self, local_order: Order):
        """
        Query order and handling state changes if needed.
        """
        try:
            remote_order = await self.order_execution_strategy.get_order(local_order.identifier, local_order.symbol)
            await self._handle_order_status_change(remote_order)

        except Exception as error:
            self.logger.error(
                f"Failed to query remote order with identifier {local_order.identifier}: {error}",
                exc_info=True,
            )

    async def _handle_order_status_change(
        self,
        remote_order: Order,
    ) -> None:
        """
        Handles changes in the status of the order data fetched from the exchange.

        Args:
            remote_order: The latest `Order` object fetched from the exchange.

        Raises:
            ValueError: If critical fields (e.g., status) are missing from the remote order.
        """
        try:
            if remote_order.status == OrderStatus.UNKNOWN:
                self.logger.error(f"Missing 'status' in remote order object: {remote_order}", exc_info=True)
                raise ValueError("Order data from the exchange is missing the 'status' field.")
            elif remote_order.status == OrderStatus.CLOSED:
                self.order_book.update_order_status(remote_order.identifier, OrderStatus.CLOSED)
                await self.event_bus.publish(Events.ORDER_FILLED, remote_order)
                self.logger.info(f"Order {remote_order.identifier} filled.")
            elif remote_order.status == OrderStatus.CANCELED:
                self.order_book.update_order_status(remote_order.identifier, OrderStatus.CANCELED)
                await self.event_bus.publish(Events.ORDER_CANCELLED, remote_order)
                self.logger.warning(f"Order {remote_order.identifier} was canceled.")
            elif remote_order.status == OrderStatus.OPEN:  # Still open
                if remote_order.filled > 0:
                    self.logger.info(
                        f"Order {remote_order} partially filled. Filled: {remote_order.filled}, "
                        f"Remaining: {remote_order.remaining}.",
                    )
                else:
                    self.logger.info(f"Order {remote_order} is still open. No fills yet.")
            else:
                self.logger.warning(
                    f"Unhandled order status '{remote_order.status}' for order {remote_order.identifier}.",
                )

        except Exception as e:
            self.logger.error(f"Error handling order status change: {e}", exc_info=True)

    def _create_task(self, coro):
        """
        Creates a managed asyncio task and adds it to the active task set.

        Args:
            coro: Coroutine to be scheduled as a task.
        """
        task = asyncio.create_task(coro)
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        return task

    async def _cancel_active_tasks(self):
        """
        Cancels all active tasks tracked by the tracker.
        """
        for task in self._active_tasks:
            task.cancel()
        await asyncio.gather(*self._active_tasks, return_exceptions=True)
        self._active_tasks.clear()

    def start_tracking(self) -> None:
        """
        Starts the order tracking task.
        """
        if self._monitoring_task and not self._monitoring_task.done():
            self.logger.warning("OrderStatusTracker is already running.")
            return
        self._monitoring_task = asyncio.create_task(self._track_open_order_statuses())
        self.logger.info("OrderStatusTracker has started tracking open orders.")

    async def stop_tracking(self) -> None:
        """
        Stops the order tracking task.
        """
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                self.logger.info("OrderStatusTracker monitoring task was cancelled.")
            await self._cancel_active_tasks()
            self._monitoring_task = None
            self.logger.info("OrderStatusTracker has stopped tracking open orders.")
