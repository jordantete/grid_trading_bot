from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.order_handling.backtest_clock import BacktestClock
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface

from .backtest_order_execution_strategy import BacktestOrderExecutionStrategy
from .live_order_execution_strategy import LiveOrderExecutionStrategy
from .order_execution_strategy_interface import OrderExecutionStrategyInterface


class OrderExecutionStrategyFactory:
    @staticmethod
    def create(
        config_manager: ConfigManager,
        exchange_service: ExchangeInterface,
        backtest_clock: BacktestClock | None = None,
    ) -> OrderExecutionStrategyInterface:
        trading_mode = config_manager.config.exchange.trading_mode

        if trading_mode == TradingMode.LIVE or trading_mode == TradingMode.PAPER_TRADING:
            return LiveOrderExecutionStrategy(
                exchange_service=exchange_service,
                max_retries=config_manager.config.execution.max_retries,
                retry_delay=config_manager.config.execution.retry_delay,
            )
        elif trading_mode == TradingMode.BACKTEST:
            return BacktestOrderExecutionStrategy(
                slippage=config_manager.config.execution.backtest_slippage,
                clock=backtest_clock,
            )
        else:
            raise ValueError(f"Unknown trading mode: {trading_mode}")
