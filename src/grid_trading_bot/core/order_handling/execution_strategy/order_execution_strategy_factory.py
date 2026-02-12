from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface

from .backtest_order_execution_strategy import BacktestOrderExecutionStrategy
from .live_order_execution_strategy import LiveOrderExecutionStrategy
from .order_execution_strategy_interface import OrderExecutionStrategyInterface


class OrderExecutionStrategyFactory:
    @staticmethod
    def create(
        config_manager: ConfigManager,
        exchange_service: ExchangeInterface,
    ) -> OrderExecutionStrategyInterface:
        trading_mode = config_manager.get_trading_mode()

        if trading_mode == TradingMode.LIVE or trading_mode == TradingMode.PAPER_TRADING:
            return LiveOrderExecutionStrategy(exchange_service=exchange_service)
        elif trading_mode == TradingMode.BACKTEST:
            return BacktestOrderExecutionStrategy()
        else:
            raise ValueError(f"Unknown trading mode: {trading_mode}")
