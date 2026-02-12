from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.trading_mode import TradingMode

from .backtest_exchange_service import BacktestExchangeService
from .live_exchange_service import LiveExchangeService


class ExchangeServiceFactory:
    @staticmethod
    def create_exchange_service(
        config_manager: ConfigManager,
        trading_mode: TradingMode,
    ):
        if trading_mode == TradingMode.BACKTEST:
            return BacktestExchangeService(config_manager)
        elif trading_mode == TradingMode.PAPER_TRADING:
            return LiveExchangeService(config_manager, is_paper_trading_activated=True)
        elif trading_mode == TradingMode.LIVE:
            return LiveExchangeService(config_manager, is_paper_trading_activated=False)
        else:
            raise ValueError(f"Unsupported trading mode: {trading_mode}")
