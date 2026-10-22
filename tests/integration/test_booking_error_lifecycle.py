"""End to end on the CLI: a market order the exchange executed but the bot cannot book halts it and is never resent."""

import signal

import pytest

from tests.fakes.fake_exchange import ExchangeState
from tests.integration.lifecycle_helpers import (
    exchange_path,
    halt_markers,
    saved_pending,
    saved_state,
    start,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

STOP_LOSS = {
    "risk_management": {"stop_loss": {"enabled": True, "threshold": 95.0}},
    "execution": {"circuit_breaker_failure_threshold": 20},
}
# Paper keeps its own books and books at restart what the exchange executed while the bot was down.
PAPER = {"exchange": {"trading_mode": "paper_trading"}}


def _created(run, order_type=None, side=None):
    return [
        entry
        for entry in run.entries()
        if entry["method"] == "create_order" and order_type in (None, entry["type"]) and side in (None, entry["side"])
    ]


def _drop_the_halt_marker(tmp_path):
    # `resume` archives the database, so the kept entry is recovered only after a kill before the marker is written.
    for marker in halt_markers(tmp_path):
        marker.unlink()


def test_a_liquidation_sale_that_cannot_be_booked_halts_without_selling_again(tmp_path):
    run = start(tmp_path, "stop_loss", overrides=STOP_LOSS)
    run.wait_for_grid()
    exchange = ExchangeState(exchange_path(tmp_path))
    exchange.set_scenario(malformed_market_fee=True, price=94.0)

    assert run.finish(timeout=60) == 0, run.log.read_text()
    assert [entry["side"] for entry in _created(run, "market")] == ["buy", "sell"]
    assert len(halt_markers(tmp_path)) == 1
    assert "Liquidation sale not booked" in run.log.read_text()
    [pending] = saved_pending(tmp_path)
    assert (pending["purpose"], pending["sent"]) == ("liquidation", 1)

    _drop_the_halt_marker(tmp_path)
    restart = start(tmp_path, "restart")

    assert restart.finish(timeout=60) == 0, restart.log.read_text()
    assert _created(restart, "market") == []


def test_an_initial_purchase_that_cannot_be_booked_halts_and_books_once_the_answer_is_fixed(tmp_path):
    run = start(tmp_path, "purchase", scenario={"malformed_market_fee": True}, overrides=PAPER)

    assert run.finish(timeout=60) == 0, run.log.read_text()
    assert [entry["side"] for entry in _created(run, "market")] == ["buy"]
    assert _created(run, "limit") == []
    assert len(halt_markers(tmp_path)) == 1
    assert "Initial purchase not booked" in run.log.read_text()
    [pending] = saved_pending(tmp_path)
    assert (pending["purpose"], pending["sent"]) == ("initial_purchase", 1)

    exchange = ExchangeState(exchange_path(tmp_path))
    exchange.repair_market_fees()
    _drop_the_halt_marker(tmp_path)
    restart = start(tmp_path, "restart")
    restart.wait_for_grid()
    restart.process.send_signal(signal.SIGTERM)

    assert restart.finish() == 143, restart.log.read_text()
    assert _created(restart, "market") == []
    assert saved_pending(tmp_path) == []
    # Paper starts from the same 1000 USDT as the fake exchange, so one booked purchase leaves the same crypto on both.
    assert saved_state(tmp_path).crypto_total == pytest.approx(exchange.totals()["BTC"], abs=1e-6)


def test_an_initial_purchase_whose_booking_fails_again_at_restart_refuses_to_start(tmp_path):
    run = start(tmp_path, "purchase", scenario={"malformed_market_fee": True}, overrides=PAPER)
    assert run.finish(timeout=60) == 0, run.log.read_text()
    before = (saved_pending(tmp_path), saved_state(tmp_path))

    _drop_the_halt_marker(tmp_path)
    restart = start(tmp_path, "restart")

    assert restart.finish(timeout=60) == 0, restart.log.read_text()
    assert _created(restart) == []
    assert halt_markers(tmp_path) == []
    assert (saved_pending(tmp_path), saved_state(tmp_path)) == before
