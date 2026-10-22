from dataclasses import dataclass

import pandas as pd


def to_epoch_ms(timestamp: pd.Timestamp | int) -> int:
    """Milliseconds since the epoch: an integer is taken as already in ms, a naive Timestamp as UTC."""
    if isinstance(timestamp, pd.Timestamp):
        return int(timestamp.value // 1_000_000)
    return int(timestamp)


@dataclass
class BacktestClock:
    """The time of the candle a backtest is processing, read by the orders it creates."""

    now_ms: int = 0

    def set(self, timestamp: pd.Timestamp | int) -> None:
        self.now_ms = to_epoch_ms(timestamp)
