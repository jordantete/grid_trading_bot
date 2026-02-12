from .exceptions import (
    InsufficientBalanceError,
    InsufficientCryptoBalanceError,
    InvalidOrderQuantityError,
)


class OrderValidator:
    def __init__(self, tolerance: float = 1e-6, threshold_ratio: float = 0.5):
        """
        Initializes the OrderValidator with a specified tolerance and threshold.

        Args:
            tolerance (float): Minimum precision tolerance for validation.
            threshold_ratio (float): Threshold below which an insufficient balance/crypto error is triggered early.
        """
        self.tolerance = tolerance
        self.threshold_ratio = threshold_ratio

    def adjust_and_validate_buy_quantity(self, balance: float, order_quantity: float, price: float) -> float:
        """
        Adjusts and validates the buy order quantity based on the available balance.

        Args:
            balance (float): Available fiat balance.
            order_quantity (float): Requested buy quantity.
            price (float): Price of the asset.

        Returns:
            float: Adjusted and validated buy order quantity.

        Raises:
            InsufficientBalanceError: If the balance is insufficient to place any valid order.
            InvalidOrderQuantityError: If the adjusted quantity is invalid.
        """
        total_cost = order_quantity * price

        if balance < total_cost * self.threshold_ratio:
            raise InsufficientBalanceError(
                f"Balance {balance:.2f} is far below the required cost {total_cost:.2f} "
                f"(threshold ratio: {self.threshold_ratio}).",
            )

        if total_cost > balance:
            adjusted_quantity = max((balance - self.tolerance) / price, 0)

            if adjusted_quantity <= 0 or (adjusted_quantity * price) < self.tolerance:
                raise InsufficientBalanceError(
                    f"Insufficient balance: {balance:.2f} to place any buy order at price {price:.2f}.",
                )
        else:
            adjusted_quantity = order_quantity

        self._validate_quantity(adjusted_quantity, is_buy=True)
        return adjusted_quantity

    def adjust_and_validate_sell_quantity(self, crypto_balance: float, order_quantity: float) -> float:
        """
        Adjusts and validates the sell order quantity based on the available crypto balance.

        Args:
            crypto_balance (float): Available crypto balance.
            order_quantity (float): Requested sell quantity.

        Returns:
            float: Adjusted and validated sell order quantity.

        Raises:
            InsufficientCryptoBalanceError: If the crypto balance is insufficient to place any valid order.
            InvalidOrderQuantityError: If the adjusted quantity is invalid.
        """
        if crypto_balance < order_quantity * self.threshold_ratio:
            raise InsufficientCryptoBalanceError(
                f"Crypto balance {crypto_balance:.6f} is far below the required quantity {order_quantity:.6f} "
                f"(threshold ratio: {self.threshold_ratio}).",
            )

        adjusted_quantity = min(order_quantity, crypto_balance - self.tolerance)
        self._validate_quantity(adjusted_quantity, is_buy=False)
        return adjusted_quantity

    def _validate_quantity(self, quantity: float, is_buy: bool) -> None:
        """
        Validates the adjusted order quantity.

        Args:
            quantity (float): Adjusted quantity to validate.
            is_buy (bool): Whether the order is a buy order.

        Raises:
            InvalidOrderQuantityError: If the quantity is invalid.
        """
        if quantity <= 0:
            order_type = "buy" if is_buy else "sell"
            raise InvalidOrderQuantityError(f"Invalid {order_type} quantity: {quantity:.6f}")
