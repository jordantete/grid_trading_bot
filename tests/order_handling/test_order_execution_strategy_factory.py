from unittest.mock import Mock

import pytest

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.order_handling.execution_strategy.backtest_order_execution_strategy import (
    BacktestOrderExecutionStrategy,
)
from grid_trading_bot.core.order_handling.execution_strategy.live_order_execution_strategy import (
    LiveOrderExecutionStrategy,
)
from grid_trading_bot.core.order_handling.execution_strategy.order_execution_strategy_factory import (
    OrderExecutionStrategyFactory,
)
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface


class TestOrderExecutionStrategyFactory:
    @pytest.fixture
    def config_manager(self):
        return Mock(spec=ConfigManager)

    @pytest.fixture
    def exchange_service(self):
        return Mock(spec=ExchangeInterface)

    def test_create_live_strategy(self, config_manager, exchange_service):
        config_manager.get_trading_mode.return_value = TradingMode.LIVE
        strategy = OrderExecutionStrategyFactory.create(config_manager, exchange_service)

        assert isinstance(
            strategy,
            LiveOrderExecutionStrategy,
        ), "Expected LiveOrderExecutionStrategy instance for live trading mode"
        assert (
            strategy.exchange_service == exchange_service
        ), "Expected exchange_service to be set correctly in LiveOrderExecutionStrategy"

    def test_create_paper_trading_strategy(self, config_manager, exchange_service):
        config_manager.get_trading_mode.return_value = TradingMode.PAPER_TRADING
        strategy = OrderExecutionStrategyFactory.create(config_manager, exchange_service)

        assert isinstance(
            strategy,
            LiveOrderExecutionStrategy,
        ), "Expected LiveOrderExecutionStrategy instance for paper trading mode"
        assert (
            strategy.exchange_service == exchange_service
        ), "Expected exchange_service to be set correctly in LiveOrderExecutionStrategy"

    def test_create_backtest_strategy(self, config_manager, exchange_service):
        config_manager.get_trading_mode.return_value = TradingMode.BACKTEST
        strategy = OrderExecutionStrategyFactory.create(config_manager, exchange_service)

        assert isinstance(
            strategy,
            BacktestOrderExecutionStrategy,
        ), "Expected BacktestOrderExecutionStrategy instance for backtesting mode"

    def test_invalid_trading_mode_raises_error(self, config_manager, exchange_service):
        config_manager.get_trading_mode.return_value = "UNKNOWN_MODE"  # Simulate an invalid mode
        with pytest.raises(ValueError, match="Unknown trading mode: UNKNOWN_MODE"):
            OrderExecutionStrategyFactory.create(config_manager, exchange_service)
