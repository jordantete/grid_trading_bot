import dataclasses
from unittest.mock import Mock

import pytest
from tests.fakes.config import config_manager_from

from grid_trading_bot.core.order_handling.backtest_clock import BacktestClock
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
    def exchange_service(self):
        return Mock(spec=ExchangeInterface)

    def test_create_live_strategy(self, valid_config, exchange_service):
        config_manager = config_manager_from(valid_config, {"exchange": {"trading_mode": "live"}})
        strategy = OrderExecutionStrategyFactory.create(config_manager, exchange_service)

        assert isinstance(
            strategy,
            LiveOrderExecutionStrategy,
        ), "Expected LiveOrderExecutionStrategy instance for live trading mode"
        assert strategy.exchange_service == exchange_service, (
            "Expected exchange_service to be set correctly in LiveOrderExecutionStrategy"
        )

    def test_create_paper_trading_strategy(self, valid_config, exchange_service):
        config_manager = config_manager_from(valid_config, {"exchange": {"trading_mode": "paper_trading"}})
        strategy = OrderExecutionStrategyFactory.create(config_manager, exchange_service)

        assert isinstance(
            strategy,
            LiveOrderExecutionStrategy,
        ), "Expected LiveOrderExecutionStrategy instance for paper trading mode"
        assert strategy.exchange_service == exchange_service, (
            "Expected exchange_service to be set correctly in LiveOrderExecutionStrategy"
        )

    def test_create_backtest_strategy(self, valid_config, exchange_service):
        config_manager = config_manager_from(valid_config, {"exchange": {"trading_mode": "backtest"}})
        strategy = OrderExecutionStrategyFactory.create(config_manager, exchange_service)

        assert isinstance(
            strategy,
            BacktestOrderExecutionStrategy,
        ), "Expected BacktestOrderExecutionStrategy instance for backtesting mode"

    def test_the_backtest_strategy_reads_the_clock_it_is_given(self, valid_config, exchange_service):
        config_manager = config_manager_from(valid_config, {"exchange": {"trading_mode": "backtest"}})
        clock = BacktestClock()

        strategy = OrderExecutionStrategyFactory.create(config_manager, exchange_service, backtest_clock=clock)

        assert strategy.clock is clock

    def test_invalid_trading_mode_raises_error(self, valid_config, exchange_service):
        config_manager = config_manager_from(valid_config)
        # The loader refuses an unknown mode, so the defensive branch is reached by replacing the loaded value.
        config_manager.config = dataclasses.replace(
            config_manager.config,
            exchange=dataclasses.replace(config_manager.config.exchange, trading_mode="UNKNOWN_MODE"),
        )
        with pytest.raises(ValueError, match="Unknown trading mode: UNKNOWN_MODE"):
            OrderExecutionStrategyFactory.create(config_manager, exchange_service)
