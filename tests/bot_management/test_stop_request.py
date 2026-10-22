import signal

import pytest

from grid_trading_bot.core.bot_management.stop_request import (
    EXIT_CONFIG,
    EXIT_FAILURE,
    EXIT_FEED_LOST,
    EXIT_OK,
    StopKind,
    StopRequest,
    aggregate_exit_codes,
)


@pytest.mark.parametrize(
    ("kind", "halts"),
    [
        (StopKind.MANUAL, True),
        (StopKind.RISK, True),
        (StopKind.INFEASIBLE, True),
        (StopKind.PERSISTENCE, True),
        (StopKind.SIGNAL, False),
        (StopKind.FEED_LOST, False),
        (StopKind.CRASH, False),
        (StopKind.START_REFUSED, False),
        (StopKind.EXCHANGE_UNAVAILABLE, False),
    ],
)
def test_only_deliberate_stops_halt(kind, halts):
    assert kind.halts is halts


@pytest.mark.parametrize(
    ("request_", "code"),
    [
        (StopRequest(StopKind.MANUAL, "stop command"), EXIT_OK),
        (StopRequest(StopKind.RISK, "TP or SL hit."), EXIT_OK),
        (StopRequest(StopKind.INFEASIBLE, "Grid infeasible"), EXIT_OK),
        (StopRequest(StopKind.PERSISTENCE, "3 checkpoints failed in a row"), EXIT_OK),
        (StopRequest(StopKind.SIGNAL, "Received SIGINT", signal_number=signal.SIGINT), 130),
        (StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=signal.SIGTERM), 143),
        (StopRequest(StopKind.FEED_LOST, "feed died"), EXIT_FEED_LOST),
        (StopRequest(StopKind.CRASH, "boom"), EXIT_FAILURE),
        (StopRequest(StopKind.START_REFUSED, "orders still open"), EXIT_OK),
        (StopRequest(StopKind.EXCHANGE_UNAVAILABLE, "exchange down"), EXIT_FEED_LOST),
    ],
)
def test_exit_code_per_kind(request_, code):
    assert request_.exit_code == code


def test_a_signal_stop_needs_its_signal_number():
    with pytest.raises(ValueError, match="signal_number"):
        StopRequest(StopKind.SIGNAL, "Received SIGTERM")


def test_aggregate_prefers_failure_then_feed_then_signal_then_success():
    assert aggregate_exit_codes([EXIT_OK, 143, EXIT_FEED_LOST, EXIT_FAILURE]) == EXIT_FAILURE
    assert aggregate_exit_codes([EXIT_OK, 143, EXIT_FEED_LOST]) == EXIT_FEED_LOST
    assert aggregate_exit_codes([EXIT_OK, 143]) == 143
    assert aggregate_exit_codes([EXIT_OK, EXIT_OK]) == EXIT_OK
    assert aggregate_exit_codes([]) == EXIT_OK


def test_config_error_code_is_ex_config():
    assert EXIT_CONFIG == 78


def test_a_signal_request_that_lost_its_number_raises_instead_of_asserting():
    request = StopRequest(StopKind.SIGNAL, "SIGTERM", signal_number=15)
    # The only way past __post_init__ and the frozen dataclass; nothing in the bot can do this.
    object.__setattr__(request, "signal_number", None)

    with pytest.raises(RuntimeError, match="signal_number"):
        _ = request.exit_code
