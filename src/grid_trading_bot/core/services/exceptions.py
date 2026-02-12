class UnsupportedExchangeError(Exception):
    """Raised when the exchange is not supported."""

    pass


class DataFetchError(Exception):
    """Raised when data fetching fails after retries."""

    pass


class HistoricalMarketDataFileNotFoundError(Exception):
    """Raised when historical market data has not been found in repository."""

    pass


class UnsupportedTimeframeError(Exception):
    """Raised when a timeframe is not supported by a given exchange."""

    pass


class UnsupportedPairError(Exception):
    """Raised when a crypto pair is not supported by a given exchange."""

    pass


class OrderCancellationError(Exception):
    """Raised when order cancellation fails."""

    pass


class MissingEnvironmentVariableError(Exception):
    """Raised when env variable are missing (EXCHANGE_API_KEY and/or EXCHANGE_SECRET_KEY)."""

    pass
