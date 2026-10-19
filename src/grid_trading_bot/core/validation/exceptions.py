from grid_trading_bot.core.exceptions import OrderRefusedError


class InsufficientBalanceError(OrderRefusedError):
    """Raised when balance is insufficient to place a buy or sell order."""

    pass


class InsufficientCryptoBalanceError(OrderRefusedError):
    """Raised when crypto balance is insufficient to complete a sell order."""

    pass


class InvalidOrderQuantityError(OrderRefusedError):
    """Raised when order quantity (amount) is invalid."""

    pass
