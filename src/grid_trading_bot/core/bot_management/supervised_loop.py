import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from typing import Any

from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest

Notify = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class LoopState:
    running: bool
    consecutive_failures: int
    last_error: str | None
    cancelled: bool = False


class SupervisedLoop:
    """
    Runs `body` every `interval` seconds. An exception from the body is logged and alerted at the first failure of a
    streak, and the loop runs again; a success after failures sends LOOP_RECOVERED. After MAX_CONSECUTIVE_FAILURES in
    a row the loop requests a CRASH stop (orders kept, the supervisor relaunches the bot) and ends.
    """

    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(
        self,
        name: str,
        interval: float,
        body: Callable[[], Awaitable[None]],
        *,
        run_first: bool,
        event_bus: EventBus | None,
        notify: Notify | None,
    ) -> None:
        self.name = name
        self.interval = interval
        self._body = body
        self._run_first = run_first
        self._event_bus = event_bus
        self._notify = notify
        self._running = False
        self._cancelled = False
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self.logger = logging.getLogger(self.__class__.__name__)

    @property
    def state(self) -> LoopState:
        return LoopState(self._running, self._consecutive_failures, self._last_error, self._cancelled)

    async def run(self) -> None:
        """Runs until cancelled, which propagates, or until it gives up after too many failures in a row."""
        self._running = True
        try:
            if not self._run_first:
                await asyncio.sleep(self.interval)
            while True:
                try:
                    await self._body()
                except asyncio.CancelledError as error:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise
                    if await self._record_failure(error):
                        return
                except Exception as error:
                    if await self._record_failure(error):
                        return
                else:
                    await self._record_success()
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            self._cancelled = True
            raise
        finally:
            self._running = False

    async def _record_failure(self, error: BaseException) -> bool:
        """Counts a failed pass and alerts or gives up. Returns True when the loop must end."""
        self._consecutive_failures += 1
        self._last_error = f"{type(error).__name__}: {error}"
        self.logger.error(
            f"{self.name} failed ({self._consecutive_failures} in a row): {self._last_error}", exc_info=error
        )
        if self._consecutive_failures == 1:
            await self._send(
                NotificationType.ERROR_OCCURRED,
                error_details=(
                    f"{self.name} failed: {self._last_error}. It runs again in {self.interval:g} s; the bot stops, "
                    f"keeping its orders, after {self.MAX_CONSECUTIVE_FAILURES} failures in a row."
                ),
            )
        if self._consecutive_failures < self.MAX_CONSECUTIVE_FAILURES:
            return False
        reason = f"{self.name} failed {self._consecutive_failures} times in a row: {self._last_error}"
        if self._event_bus is None:
            self.logger.error(f"{reason}; no event bus to request a stop, so only the loop ends.")
        else:
            # Published from its own task: the shutdown it runs awaits the task this loop runs in.
            self._stop_task = asyncio.create_task(
                self._event_bus.publish(Events.STOP_BOT, StopRequest(StopKind.CRASH, reason))
            )
        return True

    async def _record_success(self) -> None:
        if not self._consecutive_failures:
            return
        failures, self._consecutive_failures = self._consecutive_failures, 0
        self._last_error = None
        self.logger.info(f"{self.name} runs again after {failures} failed pass(es).")
        await self._send(NotificationType.LOOP_RECOVERED, loop=self.name, failures=failures)

    async def _send(self, notification_type: NotificationType, **details: Any) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(notification_type, **details)
        except Exception as error:
            self.logger.error(f"{self.name} alert {notification_type.name} could not be sent: {error}", exc_info=True)
