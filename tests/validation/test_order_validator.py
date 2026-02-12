import pytest

from grid_trading_bot.core.validation.exceptions import (
    InsufficientBalanceError,
    InsufficientCryptoBalanceError,
    InvalidOrderQuantityError,
)
from grid_trading_bot.core.validation.order_validator import OrderValidator


class TestOrderValidator:
    @pytest.fixture
    def validator(self):
        return OrderValidator()

    def test_adjust_and_validate_buy_quantity_valid(self, validator):
        balance = 5000
        order_quantity = 1
        price = 3000

        adjusted_quantity = validator.adjust_and_validate_buy_quantity(balance, order_quantity, price)
        assert adjusted_quantity == order_quantity

    def test_adjust_and_validate_buy_quantity_adjusted(self, validator):
        balance = 2000  # Insufficient for full quantity
        order_quantity = 1
        price = 3000

        adjusted_quantity = validator.adjust_and_validate_buy_quantity(balance, order_quantity, price)
        expected_quantity = (balance - validator.tolerance) / price
        assert adjusted_quantity == pytest.approx(expected_quantity, rel=1e-6)

    def test_adjust_and_validate_buy_quantity_insufficient_balance(self, validator):
        balance = 10  # Far below required cost
        order_quantity = 1
        price = 3000

        with pytest.raises(InsufficientBalanceError, match="far below the required cost"):
            validator.adjust_and_validate_buy_quantity(balance, order_quantity, price)

    def test_adjust_and_validate_buy_quantity_invalid_quantity(self, validator):
        balance = 5000
        order_quantity = -1  # Invalid quantity
        price = 3000

        with pytest.raises(InvalidOrderQuantityError, match="Invalid buy quantity"):
            validator.adjust_and_validate_buy_quantity(balance, order_quantity, price)

    def test_adjust_and_validate_sell_quantity_valid(self, validator):
        crypto_balance = 5
        order_quantity = 3

        adjusted_quantity = validator.adjust_and_validate_sell_quantity(crypto_balance, order_quantity)
        assert adjusted_quantity == order_quantity

    def test_adjust_and_validate_sell_quantity_adjusted(self, validator):
        crypto_balance = 2  # Insufficient for full quantity
        order_quantity = 3

        adjusted_quantity = validator.adjust_and_validate_sell_quantity(crypto_balance, order_quantity)
        expected_quantity = crypto_balance - validator.tolerance
        assert adjusted_quantity == pytest.approx(expected_quantity, rel=1e-6)

    def test_adjust_and_validate_sell_quantity_insufficient_balance(self, validator):
        crypto_balance = 0.1  # Far below required amount
        order_quantity = 3

        with pytest.raises(InsufficientCryptoBalanceError, match="far below the required quantity"):
            validator.adjust_and_validate_sell_quantity(crypto_balance, order_quantity)

    def test_adjust_and_validate_sell_quantity_invalid_quantity(self, validator):
        crypto_balance = 5
        order_quantity = -3  # Invalid quantity

        with pytest.raises(InvalidOrderQuantityError, match="Invalid sell quantity"):
            validator.adjust_and_validate_sell_quantity(crypto_balance, order_quantity)

    def test_adjust_and_validate_buy_quantity_tolerance_threshold(self, validator):
        balance = 1400  # Just above tolerance threshold
        order_quantity = 1
        price = 3000

        with pytest.raises(InsufficientBalanceError, match="Balance .* is far below the required cost .*"):
            validator.adjust_and_validate_buy_quantity(balance, order_quantity, price)

    def test_adjust_and_validate_sell_quantity_tolerance_threshold(self, validator):
        crypto_balance = 0.001  # Just above tolerance threshold
        order_quantity = 3

        with pytest.raises(
            InsufficientCryptoBalanceError,
            match="Crypto balance .* is far below the required quantity .*",
        ):
            validator.adjust_and_validate_sell_quantity(crypto_balance, order_quantity)
