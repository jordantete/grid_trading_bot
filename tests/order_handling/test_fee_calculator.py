from unittest.mock import Mock

import pytest

from grid_trading_bot.core.order_handling.fee_calculator import FeeCalculator
from grid_trading_bot.core.order_handling.order import Liquidity


class TestFeeCalculator:
    """Single-rate config: `maker_fee`/`taker_fee` absent, both fall back to `trading_fee`."""

    @pytest.fixture
    def config_manager(self):
        mock_config = Mock()
        mock_config.get_trading_fee.return_value = 0.001  # 0.1% trading fee
        mock_config.get_maker_fee.return_value = 0.001
        mock_config.get_taker_fee.return_value = 0.001
        return mock_config

    @pytest.fixture
    def fee_calculator(self, config_manager):
        return FeeCalculator(config_manager)

    def test_calculate_fee_basic(self, fee_calculator):
        trade_value = 1000
        expected_fee = 1  # 0.1% of 1000
        assert fee_calculator.calculate_fee(trade_value, Liquidity.MAKER) == pytest.approx(expected_fee)

    def test_calculate_fee_zero(self, fee_calculator):
        trade_value = 0
        expected_fee = 0
        assert fee_calculator.calculate_fee(trade_value, Liquidity.MAKER) == expected_fee

    def test_calculate_fee_small_value(self, fee_calculator):
        trade_value = 0.01  # 1 cent trade
        expected_fee = 0.00001  # 0.1% of 0.01
        assert fee_calculator.calculate_fee(trade_value, Liquidity.MAKER) == pytest.approx(expected_fee, rel=1e-5)

    def test_calculate_fee_large_value(self, fee_calculator):
        trade_value = 1_000_000  # 1 million trade
        expected_fee = 1000  # 0.1% of 1 million
        assert fee_calculator.calculate_fee(trade_value, Liquidity.MAKER) == pytest.approx(expected_fee)

    def test_single_rate_config_charges_makers_and_takers_alike(self, fee_calculator):
        assert fee_calculator.calculate_fee(1000, Liquidity.MAKER) == pytest.approx(
            fee_calculator.calculate_fee(1000, Liquidity.TAKER),
        )

    def test_trading_fee_from_config(self, config_manager, fee_calculator):
        assert fee_calculator.trading_fee == config_manager.get_trading_fee()

    def test_calculate_fee_tiny_trade_value_case(self, fee_calculator):
        trade_value = 0.0000001
        expected_fee = trade_value * 0.001
        assert fee_calculator.calculate_fee(trade_value, Liquidity.MAKER) == pytest.approx(expected_fee, rel=1e-9)


class TestFeeCalculatorMakerTaker:
    """Fees must be charged at the rate matching the fill's liquidity role."""

    @pytest.fixture
    def config_manager(self):
        mock_config = Mock()
        mock_config.get_trading_fee.return_value = 0.001
        mock_config.get_maker_fee.return_value = 0.0016  # Kraken-like spot maker
        mock_config.get_taker_fee.return_value = 0.0026  # Kraken-like spot taker
        return mock_config

    @pytest.fixture
    def fee_calculator(self, config_manager):
        return FeeCalculator(config_manager)

    def test_maker_fill_is_charged_the_maker_rate(self, fee_calculator):
        assert fee_calculator.calculate_fee(1000, Liquidity.MAKER) == pytest.approx(1.6)

    def test_taker_fill_is_charged_the_taker_rate(self, fee_calculator):
        assert fee_calculator.calculate_fee(1000, Liquidity.TAKER) == pytest.approx(2.6)

    def test_zero_maker_fee_is_honoured_rather_than_falling_back(self, config_manager):
        config_manager.get_maker_fee.return_value = 0.0
        calculator = FeeCalculator(config_manager)
        assert calculator.calculate_fee(1000, Liquidity.MAKER) == 0.0
        assert calculator.calculate_fee(1000, Liquidity.TAKER) == pytest.approx(2.6)

    def test_liquidity_argument_is_required(self, fee_calculator):
        with pytest.raises(TypeError):
            fee_calculator.calculate_fee(1000)
