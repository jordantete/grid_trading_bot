import dataclasses
from unittest.mock import AsyncMock, Mock

import pytest

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.schema import DynamicSpacingConfig, TrailingStopLossConfig
from grid_trading_bot.core.bot_management.event_bus import EventBus
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.order_handling.backtest_clock import BacktestClock
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.order_handling.order_simulator import OrderSimulator
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface
from grid_trading_bot.strategies.backtest_grid_strategy import BacktestGridStrategy
from grid_trading_bot.strategies.grid_trading_strategy import GridTradingStrategy
from grid_trading_bot.strategies.live_grid_strategy import LiveGridStrategy
from grid_trading_bot.strategies.plotter import Plotter
from grid_trading_bot.strategies.trading_performance_analyzer import TradingPerformanceAnalyzer
from tests.fakes.config import bot_config_from


def use_dynamic_spacing(strategy, **fields):
    config = strategy.config_manager.config
    grid_strategy = dataclasses.replace(
        config.grid_strategy, dynamic_spacing=DynamicSpacingConfig(enabled=True, **fields)
    )
    strategy.config_manager.config = dataclasses.replace(config, grid_strategy=grid_strategy)


def use_trailing_stop(strategy, **fields):
    config = strategy.config_manager.config
    risk_management = dataclasses.replace(
        config.risk_management, trailing_stop_loss=TrailingStopLossConfig(enabled=True, **fields)
    )
    strategy.config_manager.config = dataclasses.replace(config, risk_management=risk_management)


def build_strategy(
    strategy_class: type[GridTradingStrategy],
    *,
    config_manager,
    exchange_service,
    grid_manager,
    order_manager,
    balance_tracker,
    trading_performance_analyzer,
    plotter,
    event_bus,
    order_simulator,
    mode_label: str = "live",
) -> GridTradingStrategy:
    """Builds the strategy class with the order manager's fail-fast setting its mode gives it in the bot."""
    shared = {
        "config_manager": config_manager,
        "event_bus": event_bus,
        "exchange_service": exchange_service,
        "grid_manager": grid_manager,
        "order_manager": order_manager,
        "balance_tracker": balance_tracker,
        "trading_performance_analyzer": trading_performance_analyzer,
        "trading_pair": "BTC/USDT",
    }
    if strategy_class is LiveGridStrategy:
        order_manager.fail_fast = False
        return LiveGridStrategy(**shared, mode_label=mode_label)
    order_manager.fail_fast = True
    return BacktestGridStrategy(
        **shared, plotter=plotter, order_simulator=order_simulator, backtest_clock=BacktestClock()
    )


def _collaborators(valid_config, overrides: dict) -> dict:
    config_manager = Mock(spec=ConfigManager)
    config_manager.config = bot_config_from(valid_config, overrides)
    order_simulator = Mock(spec=OrderSimulator)
    order_simulator.simulate_order_fills = AsyncMock()
    return {
        "config_manager": config_manager,
        "exchange_service": Mock(spec=ExchangeInterface),
        "grid_manager": Mock(spec=GridManager),
        "order_manager": Mock(spec=OrderManager),
        "balance_tracker": Mock(spec=BalanceTracker),
        "trading_performance_analyzer": Mock(spec=TradingPerformanceAnalyzer),
        "plotter": Mock(spec=Plotter),
        "event_bus": Mock(spec=EventBus),
        "order_simulator": order_simulator,
    }


def _fixture_strategy(valid_config, strategy_class: type[GridTradingStrategy]) -> GridTradingStrategy:
    """
    Builds a real strategy with mocked collaborators, attached directly as attributes (config_manager,
    grid_manager, order_manager, balance_tracker, etc.) since the strategy stores each constructor argument.
    """
    parts = _collaborators(valid_config, {"trading_settings": {"timeframe": "1d"}})
    parts["event_bus"].publish = AsyncMock()
    parts["order_manager"].initialize_grid_orders = AsyncMock()
    parts["order_manager"].execute_take_profit_or_stop_loss_order = AsyncMock()
    return build_strategy(strategy_class, **parts)


@pytest.fixture
def strategy_fixture(valid_config):
    return _fixture_strategy(valid_config, BacktestGridStrategy)


@pytest.fixture
def live_strategy_fixture(valid_config):
    return _fixture_strategy(valid_config, LiveGridStrategy)


@pytest.fixture
def setup_strategy(valid_config):
    """
    Returns a factory building the given strategy class on one set of mocked collaborators, followed by those
    collaborators.
    """
    parts = _collaborators(
        valid_config,
        {
            "trading_settings": {"timeframe": "1d"},
            "risk_management": {
                "take_profit": {"enabled": True, "threshold": 20000},
                "stop_loss": {"enabled": True, "threshold": 10000},
            },
        },
    )

    def create_strategy(strategy_class: type[GridTradingStrategy] = BacktestGridStrategy, mode_label: str = "live"):
        return build_strategy(strategy_class, **parts, mode_label=mode_label)

    return (
        create_strategy,
        parts["config_manager"],
        parts["exchange_service"],
        parts["grid_manager"],
        parts["order_manager"],
        parts["balance_tracker"],
        parts["trading_performance_analyzer"],
        parts["plotter"],
        parts["event_bus"],
    )
