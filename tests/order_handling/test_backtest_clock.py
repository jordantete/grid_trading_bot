import pandas as pd

from grid_trading_bot.core.order_handling.backtest_clock import BacktestClock, to_epoch_ms


def test_a_naive_candle_time_is_read_as_utc_milliseconds():
    assert to_epoch_ms(pd.Timestamp("2024-08-01 00:01:00")) == 1722470460000


def test_an_aware_candle_time_gives_the_same_milliseconds():
    assert to_epoch_ms(pd.Timestamp("2024-08-01 02:01:00", tz="Europe/Paris")) == 1722470460000


def test_an_integer_is_already_milliseconds():
    assert to_epoch_ms(1722470460000) == 1722470460000


def test_the_clock_starts_at_zero_and_follows_the_candle():
    clock = BacktestClock()
    assert clock.now_ms == 0

    clock.set(pd.Timestamp("2024-08-01 00:01:00"))

    assert clock.now_ms == 1722470460000
