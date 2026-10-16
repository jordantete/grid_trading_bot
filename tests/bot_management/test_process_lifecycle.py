import asyncio
import os
import signal
from unittest.mock import AsyncMock, Mock, patch

import pytest

from grid_trading_bot.core.bot_management.process_lifecycle import ProcessLifecycle
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest


def _bot():
    bot = Mock()
    bot.request_stop = AsyncMock()
    return bot


async def _settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sigterm_suspends_every_bot():
    lifecycle = ProcessLifecycle()
    first, second = _bot(), _bot()
    lifecycle.register(first)
    lifecycle.register(second)

    lifecycle._on_terminate(signal.SIGTERM)
    await _settle()

    expected = StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=signal.SIGTERM)
    first.request_stop.assert_awaited_once_with(expected)
    second.request_stop.assert_awaited_once_with(expected)


@pytest.mark.asyncio
async def test_sigusr1_halts_every_bot():
    lifecycle = ProcessLifecycle()
    bot = _bot()
    lifecycle.register(bot)

    lifecycle._on_halt()
    await _settle()

    bot.request_stop.assert_awaited_once_with(StopRequest(StopKind.MANUAL, "Received SIGUSR1"))


@pytest.mark.asyncio
async def test_a_second_terminating_signal_exits_at_once():
    lifecycle = ProcessLifecycle()
    lifecycle.register(_bot())

    with patch("grid_trading_bot.core.bot_management.process_lifecycle.os._exit") as mock_exit:
        lifecycle._on_terminate(signal.SIGINT)
        lifecycle._on_terminate(signal.SIGINT)

    mock_exit.assert_called_once_with(130)


@pytest.mark.asyncio
async def test_bot_registered_after_a_signal_is_stopped_at_once():
    lifecycle = ProcessLifecycle()
    lifecycle._on_terminate(signal.SIGTERM)
    late = _bot()

    lifecycle.register(late)
    await _settle()

    assert late.request_stop.await_args.args[0].kind is StopKind.SIGNAL


@pytest.mark.asyncio
async def test_an_unregistered_bot_gets_nothing():
    lifecycle = ProcessLifecycle()
    bot = _bot()
    lifecycle.register(bot)
    lifecycle.unregister(bot)

    lifecycle._on_halt()
    await _settle()

    bot.request_stop.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_install_routes_a_real_signal_and_uninstall_removes_it():
    lifecycle = ProcessLifecycle()
    bot = _bot()
    lifecycle.register(bot)
    lifecycle.install(asyncio.get_running_loop())
    try:
        os.kill(os.getpid(), signal.SIGUSR1)
        for _ in range(50):
            if bot.request_stop.await_count:
                break
            await asyncio.sleep(0.01)
    finally:
        lifecycle.uninstall()

    bot.request_stop.assert_awaited_once_with(StopRequest(StopKind.MANUAL, "Received SIGUSR1"))
    assert asyncio.get_running_loop().remove_signal_handler(signal.SIGUSR1) is False
