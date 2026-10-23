import asyncio
from collections.abc import Awaitable, Callable
import inspect
import logging
from typing import Any

Subscriber = Callable[[Any], None] | Callable[[Any], Awaitable[None]]


class Events:
    """
    Defines event types for the EventBus.
    """

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
        self.subscribers: dict[str, list[Subscriber]] = {}

    def subscribe(
        self,
        event_type: str,
        callback: Subscriber,
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

    def unsubscribe(
        self,
        event_type: str,
        callback: Subscriber,
    ) -> None:
        """
        Unsubscribes a callback from a specific event type.

        Args:
            event_type: The type of event to unsubscribe from.
            callback: The callback function to remove.
        """
        if event_type in self.subscribers:
            try:
                self.subscribers[event_type].remove(callback)
                callback_name = getattr(callback, "__name__", str(callback))
                self.logger.info(f"Callback '{callback_name}' unsubscribed from event: {event_type}")
            except ValueError:
                callback_name = getattr(callback, "__name__", str(callback))
                self.logger.warning(f"Callback '{callback_name}' was not subscribed to event: {event_type}")

    async def publish(
        self,
        event_type: str,
        data: Any = None,
    ) -> None:
        """
        Publishes an event: sync subscribers run on the loop thread, async ones are awaited together.
        """
        if event_type not in self.subscribers:
            self.logger.warning(f"No subscribers for event: {event_type}")
            return

        self.logger.info(f"Publishing event: {event_type} with data: {data}")
        tasks = []
        for callback in list(self.subscribers[event_type]):
            if inspect.iscoroutinefunction(callback):
                tasks.append(self._safe_invoke_async(callback, data))
            else:
                self._safe_invoke_sync(callback, data)
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    self.logger.error(f"Exception in async event callback: {result}", exc_info=result)

    async def _safe_invoke_async(
        self,
        callback: Callable[[Any], Awaitable[Any]],
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
        callback: Callable[[Any], object],
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
