"""Each mode builds its components with the behaviour it needs, decided once where the bot is assembled."""

from types import SimpleNamespace

import pytest
from tests.fakes.fake_exchange import ExchangeState, FakeExchange
from tests.fakes.trading_stack import stack_config

from grid_trading_bot import cli
from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.bot_management.event_bus import EventBus
from grid_trading_bot.core.bot_management.grid_trading_bot import GridTradingBot
from grid_trading_bot.core.order_handling.balance_source import BalanceSource
from grid_trading_bot.core.services import live_exchange_service


@pytest.fixture
def fake_exchange(tmp_path, monkeypatch):
    state = ExchangeState.create(tmp_path / "exchange.json", price=115.0, quote=1000.0)
    monkeypatch.setattr(FakeExchange, "state_path", state.path)
    monkeypatch.setattr(live_exchange_service, "ccxtpro", SimpleNamespace(binance=FakeExchange))
    monkeypatch.setenv("EXCHANGE_API_KEY", "test")
    monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test")
    return state


def _config_manager(tmp_path, mode: str) -> ConfigManager:
    raw = stack_config(trading_mode=mode)
    raw["persistence"] = {"enabled": True, "db_path": str(tmp_path / "data" / "state.db")}
    return ConfigManager.from_dict(raw)


@pytest.mark.parametrize(
    ("mode", "fail_fast", "source", "alerts"),
    [
        ("backtest", True, BalanceSource.VIRTUAL, False),
        ("paper_trading", False, BalanceSource.VIRTUAL, True),
        ("live", False, BalanceSource.EXCHANGE, True),
    ],
)
def test_each_mode_builds_its_components_with_its_behaviour(
    tmp_path, monkeypatch, fake_exchange, mode, fail_fast, source, alerts
):
    monkeypatch.setenv("APPRISE_NOTIFICATION_URLS", "json://localhost")
    config_manager = _config_manager(tmp_path, mode)

    handler = cli.initialize_notification_handler(config_manager)
    bot = GridTradingBot("config.json", config_manager, handler, EventBus(), no_plot=True)

    assert bot.order_manager.fail_fast is fail_fast
    assert bot.balance_tracker.balance_source is source
    if mode != "backtest":
        assert bot.state_recovery_service.balance_source is source
    assert handler.enabled is alerts
