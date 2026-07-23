import math

import pytest

from grid_trading_bot.core.risk_management.trailing_stop_loss import TrailingStopLoss


class TestUpdate:
    def test_initializes_on_first_update(self):
        tsl = TrailingStopLoss(atr_multiplier=2.0)
        assert tsl.stop_price is None
        tsl.update(close=100.0, atr=3.0)
        assert tsl.stop_price == pytest.approx(94.0)  # 100 - 2*3

    def test_ratchets_up_never_down(self):
        tsl = TrailingStopLoss(atr_multiplier=2.0)
        tsl.update(close=100.0, atr=3.0)  # stop = 94
        tsl.update(close=110.0, atr=3.0)  # stop = 104
        assert tsl.stop_price == pytest.approx(104.0)
        tsl.update(close=105.0, atr=3.0)  # candidate 99 < 104 => unchanged
        assert tsl.stop_price == pytest.approx(104.0)

    def test_noop_on_nan_atr(self):
        tsl = TrailingStopLoss(atr_multiplier=2.0)
        tsl.update(close=100.0, atr=math.nan)
        assert tsl.stop_price is None

    def test_noop_on_zero_or_negative_atr(self):
        tsl = TrailingStopLoss(atr_multiplier=2.0)
        tsl.update(close=100.0, atr=0.0)
        tsl.update(close=100.0, atr=-1.0)
        assert tsl.stop_price is None


class TestTrigger:
    def test_not_triggered_before_initialization(self):
        tsl = TrailingStopLoss(atr_multiplier=2.0)
        assert tsl.is_triggered(50.0) is False

    def test_triggered_at_or_below_stop(self):
        tsl = TrailingStopLoss(atr_multiplier=2.0)
        tsl.update(close=100.0, atr=3.0)  # stop = 94
        assert tsl.is_triggered(94.0) is True
        assert tsl.is_triggered(93.0) is True
        assert tsl.is_triggered(95.0) is False

    def test_reset_clears_stop(self):
        tsl = TrailingStopLoss(atr_multiplier=2.0)
        tsl.update(close=100.0, atr=3.0)
        tsl.reset()
        assert tsl.stop_price is None
        assert tsl.is_triggered(1.0) is False


class TestSerialization:
    def test_round_trip(self):
        tsl = TrailingStopLoss(atr_multiplier=2.5)
        tsl.update(close=100.0, atr=4.0)
        restored = TrailingStopLoss.from_dict(tsl.to_dict())
        assert restored.atr_multiplier == 2.5
        assert restored.stop_price == pytest.approx(90.0)

    def test_round_trip_uninitialized(self):
        tsl = TrailingStopLoss(atr_multiplier=2.5)
        restored = TrailingStopLoss.from_dict(tsl.to_dict())
        assert restored.stop_price is None
