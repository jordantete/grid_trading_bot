import pytest

from grid_trading_bot.config.exceptions import ConfigError, ConfigFileNotFoundError
from grid_trading_bot.core.bot_management.bot_controller.exceptions import BotControllerError, CommandParsingError
from grid_trading_bot.core.exceptions import GridBotError, OrderRefusedError
from grid_trading_bot.core.order_handling.exceptions import GridFeasibilityError, OrderExecutionFailedError
from grid_trading_bot.core.services.exceptions import (
    CircuitBreakerOpenError,
    DataFetchError,
    ExchangeServiceError,
    HistoricalMarketDataFileNotFoundError,
    MissingEnvironmentVariableError,
    OrderCancellationError,
    OrderNotFoundError,
    OrderNotPlacedError,
    OrderOutcomeUnknownError,
    OrderRejectedError,
    PostOnlyRejectedError,
    UnsupportedExchangeError,
    UnsupportedPairError,
    UnsupportedTimeframeError,
)
from grid_trading_bot.core.validation.exceptions import (
    InsufficientBalanceError,
    InsufficientCryptoBalanceError,
    InvalidOrderQuantityError,
)


@pytest.mark.parametrize(
    ("error", "base"),
    [
        (ConfigError, GridBotError),
        (ConfigFileNotFoundError, GridBotError),
        (BotControllerError, GridBotError),
        (CommandParsingError, GridBotError),
        (GridFeasibilityError, GridBotError),
        (OrderExecutionFailedError, GridBotError),
        (ExchangeServiceError, GridBotError),
        (DataFetchError, ExchangeServiceError),
        (OrderNotFoundError, DataFetchError),
        (OrderNotPlacedError, DataFetchError),
        (OrderOutcomeUnknownError, DataFetchError),
        (OrderCancellationError, ExchangeServiceError),
        (CircuitBreakerOpenError, ExchangeServiceError),
        (UnsupportedExchangeError, ExchangeServiceError),
        (UnsupportedPairError, ExchangeServiceError),
        (UnsupportedTimeframeError, ExchangeServiceError),
        (HistoricalMarketDataFileNotFoundError, ExchangeServiceError),
        (MissingEnvironmentVariableError, ExchangeServiceError),
        (OrderRefusedError, GridBotError),
        (InsufficientBalanceError, OrderRefusedError),
        (InsufficientCryptoBalanceError, OrderRefusedError),
        (InvalidOrderQuantityError, OrderRefusedError),
        (OrderRejectedError, OrderRefusedError),
        (PostOnlyRejectedError, OrderRejectedError),
    ],
)
def test_every_exception_sits_under_its_branch(error, base):
    assert issubclass(error, base)


@pytest.mark.parametrize("refusal", [InsufficientBalanceError, OrderRejectedError, PostOnlyRejectedError])
def test_a_business_refusal_is_never_taken_for_a_transient_exchange_failure(refusal):
    assert not issubclass(refusal, DataFetchError)
    assert not issubclass(refusal, ExchangeServiceError)


@pytest.mark.parametrize("failure", [OrderNotPlacedError, OrderOutcomeUnknownError, OrderNotFoundError])
def test_an_unanswered_or_unconfirmed_order_is_not_a_refusal(failure):
    assert not issubclass(failure, OrderRefusedError)
