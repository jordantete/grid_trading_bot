import logging
from unittest.mock import Mock

import pytest

from grid_trading_bot.core.order_handling.fee_calculator import Fee, FeeCalculator
from grid_trading_bot.core.order_handling.order import Liquidity, Order, OrderSide, OrderStatus, OrderType


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


def _order(
    side: OrderSide = OrderSide.BUY,
    status: OrderStatus = OrderStatus.CLOSED,
    filled: float = 1.0,
    fee=None,
    order_type: OrderType = OrderType.LIMIT,
) -> Order:
    return Order(
        identifier="o1",
        status=status,
        order_type=order_type,
        side=side,
        price=100.0,
        average=100.0,
        amount=1.0,
        filled=filled,
        remaining=1.0 - filled,
        timestamp=0,
        datetime=None,
        last_trade_timestamp=None,
        symbol="BTC/USDT",
        time_in_force="GTC",
        fee=fee,
    )


class TestFeesInTheCurrencyReceived:
    """A buy pays its fee in the base it receives, a sell in the quote it receives, as Binance (without BNB) and
    OKX charge spot fees."""

    @pytest.fixture
    def calculator(self):
        config = Mock()
        config.get_trading_fee.return_value = 0.001
        config.get_maker_fee.return_value = 0.001
        config.get_taker_fee.return_value = 0.002
        config.get_base_currency.return_value = "BTC"
        config.get_quote_currency.return_value = "USDT"
        return FeeCalculator(config)

    def test_a_buy_pays_its_fee_in_base(self, calculator):
        assert calculator.fill_fee(OrderSide.BUY, 2.0, 100.0, Liquidity.MAKER) == Fee(0.002, in_base=True)

    def test_a_market_buy_pays_the_taker_rate_in_base(self, calculator):
        assert calculator.fill_fee(OrderSide.BUY, 1.0, 100.0, Liquidity.TAKER) == Fee(0.002, in_base=True)

    def test_a_sell_pays_its_fee_in_quote(self, calculator):
        fee = calculator.fill_fee(OrderSide.SELL, 2.0, 100.0, Liquidity.TAKER)

        assert (fee.amount, fee.in_base) == (pytest.approx(0.4), False)

    def test_an_order_without_a_reported_fee_is_charged_the_model(self, calculator):
        fee = calculator.execution_fee(_order(), 1.0, 100.0)

        assert fee == calculator.fill_fee(OrderSide.BUY, 1.0, 100.0, Liquidity.MAKER)

    def test_an_ended_order_is_charged_the_fee_the_exchange_reports(self, calculator):
        order = _order(fee={"cost": 0.003, "currency": "BTC"})

        fee = calculator.execution_fee(order, 1.0, 100.0)

        assert (fee.amount, fee.in_base) == (pytest.approx(0.003), True)

    def test_the_reported_total_loses_the_model_fee_booked_for_earlier_fills(self, calculator):
        order = _order(fee={"cost": 0.003, "currency": "BTC"})

        fee = calculator.execution_fee(order, 0.6, 100.0)

        assert (fee.amount, fee.in_base) == (pytest.approx(0.003 - 0.4 * 0.001), True)

    def test_a_reported_total_below_the_fee_already_booked_charges_nothing_more(self, calculator):
        order = _order(fee={"cost": 0.0001, "currency": "BTC"})

        assert calculator.execution_fee(order, 0.5, 100.0).amount == 0.0

    def test_an_open_order_ignores_the_reported_fee(self, calculator):
        order = _order(status=OrderStatus.OPEN, filled=0.4, fee={"cost": 0.01, "currency": "BTC"})

        fee = calculator.execution_fee(order, 0.4, 100.0)

        assert (fee.amount, fee.in_base) == (pytest.approx(0.0004), True)

    def test_a_buy_fee_reported_in_quote_is_charged_in_quote(self, calculator):
        order = _order(fee={"cost": 0.15, "currency": "USDT"})

        assert calculator.execution_fee(order, 1.0, 100.0) == Fee(0.15, in_base=False)

    def test_a_fee_paid_in_another_currency_charges_neither_balance(self, calculator, caplog):
        caplog.set_level(logging.INFO)
        order = _order(fee={"cost": 0.0005, "currency": "BNB"})

        assert calculator.execution_fee(order, 1.0, 100.0) == Fee(0.0, in_base=True)
        assert "0.0005 BNB" in caplog.text

    def test_a_list_of_fees_is_summed_per_currency(self, calculator):
        order = _order(fee=[{"cost": 0.001, "currency": "BTC"}, {"cost": 0.002, "currency": "BTC"}])

        assert calculator.reported_fee(order).amount == pytest.approx(0.003)

    def test_a_negative_reported_cost_counts_as_paid(self, calculator):
        order = _order(fee={"cost": -0.003, "currency": "BTC"})

        assert calculator.reported_fee(order) == Fee(0.003, in_base=True)

    def test_an_order_without_a_fee_currency_reports_nothing(self, calculator):
        assert calculator.reported_fee(_order(fee={"cost": 0.0, "currency": None})) is None

    def test_the_base_fee_of_an_order_follows_the_model_without_a_report(self, calculator):
        assert calculator.order_base_fee(_order(filled=0.5)) == pytest.approx(0.0005)

    def test_the_base_fee_of_an_ended_order_is_the_reported_one(self, calculator):
        assert calculator.order_base_fee(_order(fee={"cost": 0.003, "currency": "BTC"})) == pytest.approx(0.003)

    @pytest.mark.parametrize("currency", ["USDT", "BNB"])
    def test_a_buy_that_paid_elsewhere_has_no_base_fee(self, calculator, currency):
        assert calculator.order_base_fee(_order(fee={"cost": 0.1, "currency": currency})) == 0.0

    def test_a_sell_has_no_base_fee(self, calculator):
        assert calculator.order_base_fee(_order(side=OrderSide.SELL)) == 0.0
