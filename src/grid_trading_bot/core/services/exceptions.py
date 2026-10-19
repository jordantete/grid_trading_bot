from grid_trading_bot.core.exceptions import GridBotError, OrderRefusedError


class ExchangeServiceError(GridBotError):
    """Base of the errors the exchange layer raises."""

    pass


class UnsupportedExchangeError(ExchangeServiceError):
    """Raised when the exchange is not supported."""

    pass


class DataFetchError(ExchangeServiceError):
    """Raised when data fetching fails after retries."""

    pass


class OrderNotFoundError(DataFetchError):
    """Raised when the exchange no longer knows an order, for instance one it archived after a cancellation."""

    pass


class OrderNotPlacedError(DataFetchError):
    """
    Raised when a placement surely did not reach the order book: the circuit breaker blocked the request, or a lookup
    by client order id confirmed the order absent. Sending it again under the same client order id is safe.
    """

    pass


class OrderOutcomeUnknownError(DataFetchError):
    """Raised when a placement may have reached the order book and no lookup by its client order id settled it."""

    pass


class HistoricalMarketDataFileNotFoundError(ExchangeServiceError):
    """Raised when historical market data has not been found in repository."""

    pass


class UnsupportedTimeframeError(ExchangeServiceError):
    """Raised when a timeframe is not supported by a given exchange."""

    pass


class UnsupportedPairError(ExchangeServiceError):
    """Raised when a crypto pair is not supported by a given exchange."""

    pass


class OrderCancellationError(ExchangeServiceError):
    """Raised when order cancellation fails."""

    pass


class OrderRejectedError(OrderRefusedError):
    """
    Raised when the exchange refuses an order for a business reason (insufficient funds, an invalid order): a normal
    answer, not an exchange fault.
    """

    pass


class PostOnlyRejectedError(OrderRejectedError):
    """
    Raised when the exchange refuses a maker-only order because it would have crossed
    the spread.

    This is an expected outcome, not a fault: the grid level is simply not placeable at
    this moment. Never a `DataFetchError`, so it is not handled as a transient exchange failure.
    """

    pass


class CircuitBreakerOpenError(ExchangeServiceError):
    """Raised when the circuit breaker is open and all API calls are blocked."""

    pass


class MissingEnvironmentVariableError(ExchangeServiceError):
    """Raised when env variable are missing (EXCHANGE_API_KEY and/or EXCHANGE_SECRET_KEY)."""

    pass
