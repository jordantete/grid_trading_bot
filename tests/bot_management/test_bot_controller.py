import asyncio
import contextlib
from unittest.mock import Mock, call, patch

import pytest

from grid_trading_bot.core.bot_management.bot_controller.bot_controller import BotController
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.grid_trading_bot import GridTradingBot


@pytest.mark.asyncio
class TestBotController:
    @pytest.fixture
    def setup_bot_controller(self):
        bot = Mock(spec=GridTradingBot)
        bot.strategy = Mock()
        bot.strategy.get_formatted_orders = Mock(return_value=[])
        bot.get_balances = Mock(return_value={})
        event_bus = Mock(spec=EventBus)
        bot_controller = BotController(bot, event_bus)
        return bot_controller, bot, event_bus

    @pytest.fixture(autouse=True)
    def setup_logging(self, setup_bot_controller):
        bot_controller, _, _ = setup_bot_controller
        bot_controller.logger = Mock()

    @pytest.fixture
    def mock_input(self):
        with patch("builtins.input") as mock_input:
            yield mock_input

    async def run_command_test(self, bot_controller, mock_input):
        """Helper method to run command listener tests."""
        listener_task = asyncio.create_task(bot_controller.command_listener())

        try:
            await asyncio.sleep(0.1)
            bot_controller._stop_listening = True

            try:
                await asyncio.wait_for(listener_task, timeout=1.0)
            except TimeoutError:
                listener_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener_task

        finally:
            if not listener_task.done():
                listener_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener_task

    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    async def test_command_listener_quit(self, mock_input, setup_bot_controller):
        bot_controller, _, event_bus = setup_bot_controller
        mock_input.return_value = "quit"
        event_bus.publish_sync = Mock()

        mock_input.side_effect = ["quit", StopIteration]

        await self.run_command_test(bot_controller, mock_input)

        event_bus.publish_sync.assert_called_once_with(Events.STOP_BOT, "User requested shutdown")
        assert bot_controller._stop_listening

    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    async def test_command_listener_orders(self, mock_input, setup_bot_controller):
        bot_controller, bot, _ = setup_bot_controller
        # Set up mock to return "orders" and then raise StopIteration
        mock_input.side_effect = ["orders", StopIteration]
        bot.strategy.get_formatted_orders.return_value = [
            ["BUY", "LIMIT", "OPEN", "50000", "0.1", "2024-01-01", "1", "0.1%"],
        ]

        await self.run_command_test(bot_controller, mock_input)

        bot.strategy.get_formatted_orders.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    async def test_command_listener_balance(self, mock_input, setup_bot_controller):
        bot_controller, bot, _ = setup_bot_controller
        mock_input.side_effect = ["balance", StopIteration]
        bot.get_balances.return_value = {"USD": 1000, "BTC": 0.1}

        await self.run_command_test(bot_controller, mock_input)

        bot.get_balances.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    async def test_command_listener_stop(self, mock_input, setup_bot_controller):
        bot_controller, _, event_bus = setup_bot_controller
        mock_input.side_effect = ["stop", StopIteration]
        event_bus.publish_sync = Mock()

        await self.run_command_test(bot_controller, mock_input)

        event_bus.publish_sync.assert_called_once_with(Events.STOP_BOT, "User issued stop command")

    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    async def test_command_listener_restart(self, mock_input, setup_bot_controller):
        bot_controller, _, event_bus = setup_bot_controller
        mock_input.side_effect = ["restart", StopIteration]
        event_bus.publish_sync = Mock()

        await self.run_command_test(bot_controller, mock_input)

        assert event_bus.publish_sync.call_count == 2
        event_bus.publish_sync.assert_any_call(Events.STOP_BOT, "User issued restart command")
        event_bus.publish_sync.assert_any_call(Events.START_BOT, "User issued restart command")

    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    async def test_command_listener_invalid_command(self, mock_input, setup_bot_controller):
        bot_controller, _, _ = setup_bot_controller
        mock_input.side_effect = ["invalid", StopIteration]

        with patch.object(bot_controller.logger, "warning") as mock_logger:
            await self.run_command_test(bot_controller, mock_input)
            mock_logger.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_stop_event(self, setup_bot_controller):
        bot_controller, _, _ = setup_bot_controller

        with patch.object(bot_controller.logger, "info") as mock_logger:
            bot_controller._handle_stop_event("Test stop reason")

            assert bot_controller._stop_listening is True
            mock_logger.assert_has_calls(
                [
                    call("Received STOP_BOT event: Test stop reason"),
                    call("Command listener stopped."),
                ],
            )
            assert mock_logger.call_count == 2

    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    async def test_command_listener_unexpected_error(self, mock_input, setup_bot_controller):
        bot_controller, _, _ = setup_bot_controller
        mock_input.side_effect = Exception("Unexpected error")

        with patch.object(bot_controller.logger, "error") as mock_logger:
            await self.run_command_test(bot_controller, mock_input)

            mock_logger.assert_called_with(
                "Unexpected error in command listener: Unexpected error",
                exc_info=True,
            )

    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    async def test_command_listener_invalid_pause_duration(self, mock_input, setup_bot_controller):
        bot_controller, _, _ = setup_bot_controller
        mock_input.side_effect = ["pause invalid", StopIteration]

        with patch.object(bot_controller.logger, "warning") as mock_logger:
            await self.run_command_test(bot_controller, mock_input)
            mock_logger.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    async def test_display_orders(self, setup_bot_controller):
        bot_controller, bot, _ = setup_bot_controller
        orders = [["BUY", "LIMIT", "OPEN", "50000", "0.1", "2024-01-01", "1", "0.1%"]]
        bot.strategy.get_formatted_orders.return_value = orders

        with patch.object(bot_controller.logger, "info") as mock_logger:
            await bot_controller._display_orders()
            assert mock_logger.call_count == 2
            bot.strategy.get_formatted_orders.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    async def test_display_balance(self, setup_bot_controller):
        bot_controller, bot, _ = setup_bot_controller
        balances = {"USD": 1000, "BTC": 0.1}
        bot.get_balances.return_value = balances

        with patch.object(bot_controller.logger, "info") as mock_logger:
            await bot_controller._display_balance()
            mock_logger.assert_any_call(f"Current balances: {balances}")
            bot.get_balances.assert_called_once()
