from grid_trading_bot.config.config_manager import ConfigManager

from .order import Liquidity


class FeeCalculator:
    """
    Charges each fill at the rate matching its liquidity role.

    Both rates fall back to the flat `exchange.trading_fee` when `maker_fee` / `taker_fee`
    are not configured, so an existing single-rate config keeps behaving exactly as before.
    """

    def __init__(
        self,
        config_manager: ConfigManager,
    ):
        self.config_manager = config_manager
        self.trading_fee: float = self.config_manager.get_trading_fee()
        self.maker_fee: float = self.config_manager.get_maker_fee()
        self.taker_fee: float = self.config_manager.get_taker_fee()

    def calculate_fee(
        self,
        trade_value: float,
        liquidity: Liquidity,
    ) -> float:
        rate = self.maker_fee if liquidity == Liquidity.MAKER else self.taker_fee
        return trade_value * rate
