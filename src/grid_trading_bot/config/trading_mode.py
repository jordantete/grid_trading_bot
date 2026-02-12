from enum import Enum


class TradingMode(Enum):
    BACKTEST = "backtest"
    PAPER_TRADING = "paper_trading"
    LIVE = "live"

    @staticmethod
    def from_string(mode_str: str):
        try:
            return TradingMode(mode_str)
        except ValueError:
            available_modes = ", ".join([mode.value for mode in TradingMode])
            raise ValueError(
                f"Invalid trading mode: '{mode_str}'. Available modes are: {available_modes}",
            ) from None
