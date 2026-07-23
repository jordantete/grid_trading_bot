import math

import pandas as pd


class ATRCalculator:
    """Average True Range with Wilder smoothing. Pure and stateless."""

    @staticmethod
    def compute_series(candles: pd.DataFrame, period: int) -> pd.Series:
        """
        Returns the full ATR series aligned with `candles`.
        The first `period` values are NaN (warm-up).
        """
        if len(candles) < period + 1:
            return pd.Series([math.nan] * len(candles), index=candles.index)

        high = candles["high"].astype(float)
        low = candles["low"].astype(float)
        close = candles["close"].astype(float)
        prev_close = close.shift(1)

        true_range = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)

        return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period + 1).mean()

    @staticmethod
    def compute(candles: pd.DataFrame, period: int) -> float:
        """Returns the latest ATR value, or NaN if fewer than period+1 candles."""
        series = ATRCalculator.compute_series(candles, period)
        if len(series) == 0:
            return math.nan
        return float(series.iloc[-1])
