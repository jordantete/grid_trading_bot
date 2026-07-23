import math

import pandas as pd
import pytest

from grid_trading_bot.core.indicators.atr_calculator import ATRCalculator


def _candles(rows: list[tuple[float, float, float]]) -> pd.DataFrame:
    """rows = [(high, low, close), ...]"""
    return pd.DataFrame(rows, columns=["high", "low", "close"])


class TestCompute:
    def test_constant_true_range_converges_to_that_range(self):
        # Every candle: high-low = 2, no gaps => TR = 2 for all candles => ATR = 2
        rows = [(101.0, 99.0, 100.0)] * 50
        atr = ATRCalculator.compute(_candles(rows), period=14)
        assert atr == pytest.approx(2.0)

    def test_flat_market_gives_zero(self):
        rows = [(100.0, 100.0, 100.0)] * 30
        atr = ATRCalculator.compute(_candles(rows), period=14)
        assert atr == pytest.approx(0.0)

    def test_insufficient_data_returns_nan(self):
        rows = [(101.0, 99.0, 100.0)] * 14  # need period+1 = 15
        atr = ATRCalculator.compute(_candles(rows), period=14)
        assert math.isnan(atr)

    def test_gap_uses_previous_close_in_true_range(self):
        # Second candle gaps up: TR = max(H-L, |H-prev_close|, |L-prev_close|)
        #                           = max(1, |111-100|, |110-100|) = 11
        rows = [(100.5, 99.5, 100.0), (111.0, 110.0, 110.5)]
        rows += [(111.0, 110.0, 110.5)] * 20
        atr = ATRCalculator.compute(_candles(rows), period=2)
        # ATR decays toward 1.0 (steady TR after the gap), must be between
        assert 1.0 < atr < 11.0


class TestComputeSeries:
    def test_series_last_value_matches_compute(self):
        rows = [(101.0 + i * 0.1, 99.0 + i * 0.1, 100.0 + i * 0.1) for i in range(40)]
        df = _candles(rows)
        series = ATRCalculator.compute_series(df, period=14)
        assert len(series) == len(df)
        assert series.iloc[-1] == pytest.approx(ATRCalculator.compute(df, period=14))

    def test_series_is_nan_during_warmup(self):
        rows = [(101.0, 99.0, 100.0)] * 40
        series = ATRCalculator.compute_series(_candles(rows), period=14)
        assert series.iloc[:14].isna().all()
        assert not math.isnan(series.iloc[20])
