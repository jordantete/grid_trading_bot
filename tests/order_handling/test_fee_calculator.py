from unittest.mock import Mock

import pytest

from grid_trading_bot.core.order_handling.fee_calculator import FeeCalculator


class TestFeeCalculator:
    @pytest.fixture
    def config_manager(self):
        mock_config = Mock()
        mock_config.get_trading_fee.return_value = 0.001  # 0.1% trading fee
        return mock_config

    @pytest.fixture
    def fee_calculator(self, config_manager):
        return FeeCalculator(config_manager)

    def test_calculate_fee_basic(self, fee_calculator):
        trade_value = 1000
        expected_fee = 1  # 0.1% of 1000
        assert fee_calculator.calculate_fee(trade_value) == pytest.approx(expected_fee)

    def test_calculate_fee_zero(self, fee_calculator):
        trade_value = 0
        expected_fee = 0
        assert fee_calculator.calculate_fee(trade_value) == expected_fee

    def test_calculate_fee_small_value(self, fee_calculator):
        trade_value = 0.01  # 1 cent trade
        expected_fee = 0.00001  # 0.1% of 0.01
        assert fee_calculator.calculate_fee(trade_value) == pytest.approx(expected_fee, rel=1e-5)

    def test_calculate_fee_large_value(self, fee_calculator):
        trade_value = 1_000_000  # 1 million trade
        expected_fee = 1000  # 0.1% of 1 million
        assert fee_calculator.calculate_fee(trade_value) == pytest.approx(expected_fee)

    def test_trading_fee_from_config(self, config_manager, fee_calculator):
        assert fee_calculator.trading_fee == config_manager.get_trading_fee()

    def test_calculate_fee_tiny_trade_value_case(self, fee_calculator):
        trade_value = 0.0000001
        expected_fee = trade_value * 0.001
        assert fee_calculator.calculate_fee(trade_value) == pytest.approx(expected_fee, rel=1e-9)
