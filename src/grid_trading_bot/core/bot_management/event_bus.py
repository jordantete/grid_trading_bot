import asyncio
from collections.abc import Awaitable, Callable
import inspect
import logging
from typing import Any


class Events:
    """
    Defines event types for the EventBus.
    """

    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    START_BOT = "start_bot"
    STOP_BOT = "stop_bot"


class EventBus:
    """
    A simple event bus for managing pub-sub interactions with support for both sync and async publishing.
    """

    def __init__(self):
        """
        Initializes the EventBus with an empty subscriber list.
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.subscribers: dict[str, list[Callable[[Any], None]]] = {}

    def subscribe(
        self,
        event_type: str,
        callback: Callable[[Any], None] | Callable[[Any], Awaitable[None]],
    ) -> None:
        """
        Subscribes a callback to a specific event type.

        Args:
            event_type: The type of event to subscribe to.
            callback: The callback function to invoke when the event is published.
        """
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []

        self.subscribers[event_type].append(callback)
        callback_name = getattr(callback, "__name__", str(callback))
        if self.logger.isEnabledFor(logging.DEBUG):
            caller_frame = inspect.stack()[1]
            caller_name = f"{caller_frame.function} (from {caller_frame.filename}:{caller_frame.lineno})"
            self.logger.debug(f"Callback '{callback_name}' subscribed to event: {event_type} by {caller_name}")
        else:
            self.logger.info(f"Callback '{callback_name}' subscribed to event: {event_type}")

    async def publish(
        self,
        event_type: str,
        data: Any = None,
    ) -> None:
        """
        Publishes an event asynchronously to all subscribers.
        """
        if event_type not in self.subscribers:
            self.logger.warning(f"No subscribers for event: {event_type}")
            return

        self.logger.info(f"Publishing async event: {event_type} with data: {data}")
        tasks = [
            self._safe_invoke_async(callback, data)
            if asyncio.iscoroutinefunction(callback)
            else asyncio.to_thread(self._safe_invoke_sync, callback, data)
            for callback in self.subscribers[event_type]
        ]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    self.logger.error(f"Exception in async event callback: {result}", exc_info=True)

    def publish_sync(
        self,
        event_type: str,
        data: Any,
    ) -> None:
        """
        Publishes an event synchronously to all subscribers.
        """
        if event_type in self.subscribers:
            self.logger.info(f"Publishing sync event: {event_type} with data: {data}")
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            for callback in self.subscribers[event_type]:
                if asyncio.iscoroutinefunction(callback):
                    if loop is not None:
                        asyncio.run_coroutine_threadsafe(self._safe_invoke_async(callback, data), loop)
                    else:
                        self.logger.warning(
                            f"No running event loop; cannot schedule async callback '{callback.__name__}'"
                        )
                else:
                    self._safe_invoke_sync(callback, data)

    async def _safe_invoke_async(
        self,
        callback: Callable[[Any], None],
        data: Any,
    ) -> None:
        """
        Safely invokes an async callback, awaiting it directly so errors propagate
        to the caller via asyncio.gather(return_exceptions=True).
        """
        self.logger.info(f"Executing async callback '{callback.__name__}' for event with data: {data}")
        await callback(data)

    def _safe_invoke_sync(
        self,
        callback: Callable[[Any], None],
        data: Any,
    ) -> None:
        """
        Safely invokes a sync callback, suppressing and logging any exceptions.
        """
        try:
            callback(data)
        except Exception as e:
            self.logger.error(f"Error in sync subscriber callback: {e}", exc_info=True)

    async def shutdown(self):
        """
        Gracefully shuts down the EventBus and clears all subscribers.
        """
        self.logger.info("Shutting down EventBus...")
        self.subscribers.clear()
        self.logger.info("EventBus shutdown complete.")
