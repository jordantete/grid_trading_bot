import asyncio
import contextlib
import io
import os
import sys
import threading
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from grid_trading_bot.core.bot_management.bot_controller.bot_controller import BotController
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.grid_trading_bot import GridTradingBot
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest


async def _listen(bot_controller, monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    await asyncio.wait_for(bot_controller.command_listener(), timeout=2)


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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["stop", "quit", "  STOP  "])
    async def test_stop_and_quit_publish_a_manual_stop(self, command, setup_bot_controller, monkeypatch):
        bot_controller, _, event_bus = setup_bot_controller
        event_bus.publish = AsyncMock()

        await _listen(bot_controller, monkeypatch, f"{command}\n")

        event_bus.publish.assert_awaited_once_with(
            Events.STOP_BOT, StopRequest(StopKind.MANUAL, "User issued stop command")
        )

    @pytest.mark.asyncio
    async def test_orders_and_balance_are_displayed(self, setup_bot_controller, monkeypatch):
        bot_controller, bot, _ = setup_bot_controller

        await _listen(bot_controller, monkeypatch, "orders\nbalance\n")

        bot.strategy.get_formatted_orders.assert_called_once()
        bot.get_balances.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["restart", "pause 5", "nonsense"])
    async def test_unknown_commands_are_rejected(self, command, setup_bot_controller, monkeypatch):
        bot_controller, _, event_bus = setup_bot_controller
        event_bus.publish = AsyncMock()

        await _listen(bot_controller, monkeypatch, f"{command}\n")

        bot_controller.logger.warning.assert_called_once()
        event_bus.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_end_of_input_ends_the_listener(self, setup_bot_controller, monkeypatch):
        bot_controller, _, _ = setup_bot_controller

        await _listen(bot_controller, monkeypatch, "")

        bot_controller.logger.info.assert_any_call("Command input closed; command listener stopped.")

    @pytest.mark.asyncio
    async def test_a_command_error_does_not_end_the_listener(self, setup_bot_controller, monkeypatch):
        bot_controller, bot, _ = setup_bot_controller
        bot.get_balances.side_effect = [Exception("boom"), {}]

        await _listen(bot_controller, monkeypatch, "balance\nbalance\n")

        assert bot.get_balances.call_count == 2

    @pytest.mark.asyncio
    async def test_the_reader_thread_never_holds_the_process(self, setup_bot_controller, monkeypatch):
        bot_controller, _, _ = setup_bot_controller
        read_fd, write_fd = os.pipe()
        monkeypatch.setattr(sys, "stdin", os.fdopen(read_fd))
        listener = asyncio.create_task(bot_controller.command_listener())
        await asyncio.sleep(0.05)

        reader = next(thread for thread in threading.enumerate() if thread.name == "CommandReader")
        assert reader.daemon is True

        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener
        os.close(write_fd)
        reader.join(timeout=2)

    @pytest.mark.asyncio
    async def test_handle_stop_event(self, setup_bot_controller):
        bot_controller, _, _ = setup_bot_controller

        with patch.object(bot_controller.logger, "info") as mock_logger:
            bot_controller._handle_stop_event(StopRequest(StopKind.MANUAL, "Test stop reason"))

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
