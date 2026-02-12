class InsufficientBalanceError(Exception):
    """Raised when balance is insufficient to place a buy or sell order."""

    pass


class InsufficientCryptoBalanceError(Exception):
    """Raised when crypto balance is insufficient to complete a sell order."""

    pass


class InvalidOrderQuantityError(Exception):
    """Raised when order quantity (amount) is invalid."""

    pass
