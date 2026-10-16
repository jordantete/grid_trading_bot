import asyncio
import logging
import os
import signal
from typing import Protocol

from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest


class StoppableBot(Protocol):
    async def request_stop(self, request: StopRequest) -> None: ...


class ProcessLifecycle:
    """
    Routes process signals to the running bots: SIGINT and SIGTERM suspend them (open orders kept),
    SIGUSR1 halts them (open orders cancelled), a second SIGINT or SIGTERM exits at once.
    """

    TERMINATING_SIGNALS = (signal.SIGINT, signal.SIGTERM)

    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bots: list[StoppableBot] = []
        self._suspend_request: StopRequest | None = None
        self._pending: set[asyncio.Task] = set()

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        for signum in self.TERMINATING_SIGNALS:
            loop.add_signal_handler(signum, self._on_terminate, signum)
        loop.add_signal_handler(signal.SIGUSR1, self._on_halt)

    def uninstall(self) -> None:
        if self._loop is None:
            return
        for signum in (*self.TERMINATING_SIGNALS, signal.SIGUSR1):
            self._loop.remove_signal_handler(signum)
        self._loop = None

    def register(self, bot: StoppableBot) -> None:
        self._bots.append(bot)
        if self._suspend_request is not None:
            self._dispatch(bot, self._suspend_request)

    def unregister(self, bot: StoppableBot) -> None:
        if bot in self._bots:
            self._bots.remove(bot)

    def _on_terminate(self, signum: int) -> None:
        name = signal.Signals(signum).name
        if self._suspend_request is not None:
            self.logger.warning(f"Second {name} received; exiting immediately.")
            os._exit(128 + signum)
        self.logger.info(f"{name} received: suspending, open orders stay on the exchange.")
        self._suspend_request = StopRequest(StopKind.SIGNAL, f"Received {name}", signal_number=signum)
        for bot in list(self._bots):
            self._dispatch(bot, self._suspend_request)

    def _on_halt(self) -> None:
        self.logger.info("SIGUSR1 received: halting, open orders are cancelled.")
        request = StopRequest(StopKind.MANUAL, "Received SIGUSR1")
        for bot in list(self._bots):
            self._dispatch(bot, request)

    def _dispatch(self, bot: StoppableBot, request: StopRequest) -> None:
        task = asyncio.get_running_loop().create_task(bot.request_stop(request))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
