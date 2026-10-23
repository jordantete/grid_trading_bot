"""Each mode builds its components with the behaviour it needs, decided once where the bot is assembled."""

from types import SimpleNamespace

import pytest
from tests.fakes.fake_exchange import ExchangeState, FakeExchange
from tests.fakes.trading_stack import stack_config

from grid_trading_bot import cli
from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.bot_management.bot_assembly import assemble_bot
from grid_trading_bot.core.bot_management.event_bus import EventBus
from grid_trading_bot.core.bot_management.grid_trading_bot import GridTradingBot
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.order_handling.balance_source import BalanceSource
from grid_trading_bot.core.services import live_exchange_service
from grid_trading_bot.strategies.backtest_grid_strategy import BacktestGridStrategy
from grid_trading_bot.strategies.live_grid_strategy import LiveGridStrategy


@pytest.fixture
def fake_exchange(tmp_path, monkeypatch):
    state = ExchangeState.create(tmp_path / "exchange.json", price=115.0, quote=1000.0)
    monkeypatch.setattr(FakeExchange, "state_path", state.path)
    monkeypatch.setattr(live_exchange_service, "ccxtpro", SimpleNamespace(binance=FakeExchange))
    monkeypatch.setenv("EXCHANGE_API_KEY", "test")
    monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test")
    return state


def _config_manager(tmp_path, mode: str, persistence: bool = True) -> ConfigManager:
    raw = stack_config(trading_mode=mode)
    raw["persistence"] = {"enabled": persistence, "db_path": str(tmp_path / "data" / "state.db")}
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


@pytest.mark.parametrize(
    ("mode", "strategy_class", "label"),
    [
        ("backtest", BacktestGridStrategy, None),
        ("paper_trading", LiveGridStrategy, "paper"),
        ("live", LiveGridStrategy, "live"),
    ],
)
def test_each_mode_builds_its_strategy(tmp_path, fake_exchange, mode, strategy_class, label):
    config_manager = _config_manager(tmp_path, mode)

    bot = GridTradingBot(
        "config.json", config_manager, NotificationHandler([], enabled=False), EventBus(), no_plot=True
    )

    assert type(bot.strategy) is strategy_class
    if label is not None:
        assert bot.strategy.mode_label == label


@pytest.mark.parametrize("persistence", [True, False])
@pytest.mark.parametrize(
    ("mode", "strategy_class", "reconciles", "persists"),
    [
        ("backtest", BacktestGridStrategy, False, False),
        ("paper_trading", LiveGridStrategy, False, True),
        ("live", LiveGridStrategy, True, True),
    ],
)
def test_assemble_bot_returns_the_components_of_the_configured_mode(
    tmp_path, fake_exchange, mode, strategy_class, reconciles, persists, persistence
):
    async def exchange_free_base() -> float:
        return 0.0

    components = assemble_bot(
        _config_manager(tmp_path, mode, persistence),
        NotificationHandler([], enabled=False),
        EventBus(),
        exchange_free_base=exchange_free_base,
    )

    assert type(components.strategy) is strategy_class
    assert (components.reconciliation_service is not None) is reconciles
    assert (components.state_persistence_service is not None) is (persists and persistence)
    assert (components.state_recovery_service is not None) is (persists and persistence)
    assert components.order_manager.journal is components.journal
    if persists:
        assert components.order_manager.exchange_balance is exchange_free_base
    else:
        assert components.order_manager.exchange_balance is None
