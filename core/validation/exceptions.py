class InsufficientBalanceError(Exception):
    """Raised when balance is insufficient to place a buy or sell order."""
    pass

class InsufficientCryptoBalanceError(Exception):
    """Raised when crypto balance is insufficient to complete a sell order."""
    pass

class GridLevelNotReadyError(Exception):
    """Raised when the grid level is not ready for a buy or sell order."""
    pass

class InvalidOrderTypeError(Exception):
    """Raised when an invalid order type is provided during order initialization."""
    pass

class InvalidOrderSideError(Exception):
    """Raised when an invalid order side is provided during order initialization."""
    pass