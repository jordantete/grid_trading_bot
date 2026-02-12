from grid_trading_bot.config.config_manager import ConfigManager


class FeeCalculator:
    def __init__(
        self,
        config_manager: ConfigManager,
    ):
        self.config_manager = config_manager
        self.trading_fee: float = self.config_manager.get_trading_fee()

    def calculate_fee(
        self,
        trade_value: float,
    ) -> float:
        return trade_value * self.trading_fee
