import asyncio
import contextvars
import logging
import sys
import threading

from tabulate import tabulate

from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.grid_trading_bot import GridTradingBot
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest

from .exceptions import CommandParsingError


class BotController:
    """
    Handles user commands and manages the lifecycle of the GridTradingBot.
    """

    def __init__(
        self,
        bot: GridTradingBot,
        event_bus: EventBus,
    ):
        """
        Initializes the BotController.

        Args:
            bot: The GridTradingBot instance to control.
            event_bus: The EventBus instance to publish/subscribe Events.
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.bot = bot
        self.event_bus = event_bus
        self._stop_listening = False
        self.event_bus.subscribe(Events.STOP_BOT, self._handle_stop_event)

    async def command_listener(self) -> None:
        """Reads commands from stdin until end of input or a stop."""
        loop = asyncio.get_running_loop()
        lines: asyncio.Queue[str | None] = asyncio.Queue()
        threading.Thread(
            target=contextvars.copy_context().run,
            args=(self._read_lines, loop, lines),
            name="CommandReader",
            daemon=True,
        ).start()
        self.logger.info("Command listener started. Commands: orders, balance, stop, quit.")

        while not self._stop_listening:
            line = await lines.get()
            if line is None:
                self.logger.info("Command input closed; command listener stopped.")
                return
            try:
                await self._handle_command(line.strip().lower())
            except CommandParsingError as e:
                self.logger.warning(f"Command error: {e}")
            except Exception as e:
                self.logger.error(f"Unexpected error in command listener: {e}", exc_info=True)

    @staticmethod
    def _read_lines(loop: asyncio.AbstractEventLoop, lines: "asyncio.Queue[str | None]") -> None:
        # A daemon thread, because a pending readline() must never keep the process alive after the bot stops.
        while True:
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError, UnicodeDecodeError, AttributeError):
                line = ""
            try:
                loop.call_soon_threadsafe(lines.put_nowait, line or None)
            except RuntimeError:
                return
            if not line:
                return

    async def _handle_command(self, command: str):
        """
        Handles individual commands from the user.

        Args:
            command: The command entered by the user.
        """
        if command in {"stop", "quit"}:
            self.logger.info("Stop command received")
            await self.event_bus.publish(Events.STOP_BOT, StopRequest(StopKind.MANUAL, "User issued stop command"))

        elif command == "orders":
            await self._display_orders()

        elif command == "balance":
            await self._display_balance()

        else:
            raise CommandParsingError(f"Unknown command: {command}")

    def cleanup(self) -> None:
        """Unsubscribes from all EventBus events."""
        self.event_bus.unsubscribe(Events.STOP_BOT, self._handle_stop_event)

    def _stop_listener(self):
        """
        Stops the command listener loop.
        """
        self._stop_listening = True
        self.logger.info("Command listener stopped.")

    def _handle_stop_event(self, request: StopRequest) -> None:
        """
        Handles the STOP_BOT event and stops the command listener.

        Args:
            request: The stop request that ends the bot.
        """
        self.logger.info(f"Received STOP_BOT event: {request.reason}")
        self._stop_listener()

    async def _display_orders(self):
        """
        Displays formatted orders retrieved from the bot.
        """
        self.logger.info("Display orders bot command received")
        formatted_orders = self.bot.strategy.get_formatted_orders()
        orders_table = tabulate(
            formatted_orders,
            headers=["Order Side", "Type", "Status", "Price", "Quantity", "Timestamp", "Grid Level", "Slippage"],
            tablefmt="pipe",
        )
        self.logger.info("\nFormatted Orders:\n" + orders_table)

    async def _display_balance(self):
        """
        Displays the current balances retrieved from the bot.
        """
        self.logger.info("Display balance bot command received")
        current_balances = self.bot.get_balances()
        self.logger.info(f"Current balances: {current_balances}")
