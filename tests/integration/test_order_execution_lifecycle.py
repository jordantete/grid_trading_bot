"""End to end on the CLI: lost answers, in-flight placements, re-placements, liquidation retries and halts."""

import signal

import pytest

from tests.fakes.fake_exchange import ExchangeState
from tests.integration.lifecycle_helpers import (
    exchange_path,
    halt_markers,
    methods,
    reserved_gap,
    saved_open_order_ids,
    saved_pending,
    saved_state,
    start,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

# A failed liquidation retries several times in a row; the breaker must not open in between.
STOP_LOSS = {
    "risk_management": {"stop_loss": {"enabled": True, "threshold": 95.0}},
    "execution": {"circuit_breaker_failure_threshold": 20},
}
TAKE_PROFIT = {"risk_management": {"take_profit": {"enabled": True, "threshold": 105.0}}}
# Six levels, 90 to 110 by 4: buys on 90 and 94, sells on 102, 106 and 110, 98 stays free.
HEDGED = {"grid_strategy": {"type": "hedged_grid", "num_grids": 6}}


def _created(run, order_type=None, side=None):
    return [
        entry
        for entry in run.entries()
        if entry["method"] == "create_order" and order_type in (None, entry["type"]) and side in (None, entry["side"])
    ]


def _failed(run, order_type=None):
    return [
        entry
        for entry in run.entries()
        if entry["method"] == "create_order_failed" and order_type in (None, entry["type"])
    ]


def _exchange_open_ids(exchange):
    return {order["id"] for order in exchange.open_orders()}


def _assert_saved_state_matches_the_exchange(tmp_path, exchange):
    assert saved_open_order_ids(tmp_path) == _exchange_open_ids(exchange)
    assert saved_pending(tmp_path) == []
    assert reserved_gap(tmp_path) == pytest.approx((0.0, 0.0), abs=1e-6)
    totals = exchange.totals()
    saved = saved_state(tmp_path)
    assert saved.fiat_total == pytest.approx(totals["USDT"], abs=1e-6)
    assert saved.crypto_total == pytest.approx(totals["BTC"], abs=1e-6)


def test_a_paired_order_whose_answer_is_lost_is_found_by_its_client_id(tmp_path):
    run = start(tmp_path, "lost")
    run.wait_for_grid()
    exchange = ExchangeState(exchange_path(tmp_path))
    exchange.fail("create_order", times=1, error="timeout", after_accept=True)
    exchange.fill(max(exchange.open_orders("buy"), key=lambda order: order["price"])["id"])

    run.wait_for(
        lambda: (
            len(exchange.open_orders("sell")) == 3 and saved_open_order_ids(tmp_path) == _exchange_open_ids(exchange)
        )
    )
    run.process.send_signal(signal.SIGTERM)
    assert run.finish() == 143, run.log.read_text()

    sells = _created(run, "limit", "sell")
    assert len(sells) == 3
    paired = sells[-1]
    assert paired["answer_lost"]
    assert [
        order for order in exchange.read()["orders"].values() if order["clientOrderId"] == paired["client_order_id"]
    ] == [exchange.read()["orders"][paired["id"]]]
    assert any(
        entry["method"] == "fetch_order_by_client_id"
        and entry["client_order_id"] == paired["client_order_id"]
        and entry["found"]
        for entry in run.entries()
    )
    _assert_saved_state_matches_the_exchange(tmp_path, exchange)


def test_a_placement_cut_off_by_sigterm_is_resolved_at_restart(tmp_path):
    first = start(tmp_path, "first")
    first.wait_for_grid()
    exchange = ExchangeState(exchange_path(tmp_path))
    exchange.set_scenario(response_delay=30.0)
    exchange.fill(max(exchange.open_orders("buy"), key=lambda order: order["price"])["id"])
    first.wait_for(lambda: len(exchange.open_orders("sell")) == 3)

    first.process.send_signal(signal.SIGTERM)
    assert first.finish(timeout=60) == 143, first.log.read_text()

    paired = _created(first, "limit", "sell")[-1]
    assert paired["id"] not in saved_open_order_ids(tmp_path)
    assert [entry["client_order_id"] for entry in saved_pending(tmp_path)] == [paired["client_order_id"]]

    exchange.set_scenario(response_delay=None)
    second = start(tmp_path, "second")
    second.wait_for(lambda: any(e["method"] == "fetch_order_by_client_id" and e["found"] for e in second.entries()))
    second.process.send_signal(signal.SIGTERM)
    assert second.finish() == 143, second.log.read_text()

    assert saved_pending(tmp_path) == []
    assert paired["id"] in saved_open_order_ids(tmp_path)
    assert "create_order" not in methods(second)
    assert len(exchange.open_orders("sell")) == 3
    _assert_saved_state_matches_the_exchange(tmp_path, exchange)


def test_an_order_cancelled_while_down_is_re_placed_at_restart(tmp_path):
    first = start(tmp_path, "first")
    first.wait_for_grid()
    first.process.send_signal(signal.SIGTERM)
    assert first.finish() == 143, first.log.read_text()

    exchange = ExchangeState(exchange_path(tmp_path))
    lowest_buy = min(exchange.open_orders("buy"), key=lambda order: order["price"])
    exchange.cancel(lowest_buy["id"])

    second = start(tmp_path, "second")
    second.wait_for(
        lambda: (
            exchange.has_open(lowest_buy["price"], "buy")
            and saved_open_order_ids(tmp_path) == _exchange_open_ids(exchange)
        )
    )
    second.process.send_signal(signal.SIGTERM)
    assert second.finish() == 143, second.log.read_text()

    assert [(e["type"], e["side"], e["price"]) for e in _created(second)] == [("limit", "buy", lowest_buy["price"])]
    assert len(exchange.open_orders("buy")) == 2
    assert len(exchange.open_orders("sell")) == 2
    _assert_saved_state_matches_the_exchange(tmp_path, exchange)


def test_a_stop_loss_liquidation_is_retried_until_it_sells(tmp_path):
    run = start(tmp_path, "stop_loss", overrides=STOP_LOSS)
    run.wait_for_grid()
    exchange = ExchangeState(exchange_path(tmp_path))
    exchange.fail("create_order", times=5, error="network")
    exchange.set_scenario(price=94.0)

    assert run.finish(timeout=60) == 0, run.log.read_text()

    failures = _failed(run, "market")
    assert len(failures) == 5
    assert {entry["side"] for entry in failures} == {"sell"}
    assert [entry["side"] for entry in _created(run, "market")] == ["buy", "sell"]
    assert exchange.open_orders() == []
    assert exchange.totals()["BTC"] == pytest.approx(0.0, abs=1e-6)
    assert len(halt_markers(tmp_path)) == 1
    assert saved_pending(tmp_path) == []


def test_a_sigterm_interrupts_the_liquidation_retries_and_the_bot_stays_halted(tmp_path):
    run = start(tmp_path, "interrupted", overrides=STOP_LOSS)
    run.wait_for_grid()
    exchange = ExchangeState(exchange_path(tmp_path))
    exchange.fail("create_order", error="network")
    exchange.set_scenario(price=94.0)
    run.wait_for(lambda: len(_failed(run, "market")) >= 2)

    run.process.send_signal(signal.SIGTERM)

    assert run.finish(timeout=60) == 0, run.log.read_text()
    assert len(halt_markers(tmp_path)) == 1
    assert exchange.open_orders() == []
    assert exchange.totals()["BTC"] > 1.0
    assert [entry["side"] for entry in _created(run, "market")] == ["buy"]
    assert "Liquidation interrupted" in run.log.read_text()


def test_a_hedged_poll_that_crosses_two_levels_places_both_paired_sells(tmp_path):
    run = start(tmp_path, "hedged", overrides=HEDGED)
    run.wait_for_limit_orders(5)
    exchange = ExchangeState(exchange_path(tmp_path))
    buys = sorted(exchange.open_orders("buy"), key=lambda order: order["price"])
    assert [order["price"] for order in buys] == [90.0, 94.0]

    exchange.fill_all([order["id"] for order in buys])

    run.wait_for(
        lambda: (
            exchange.has_open(94.0, "sell")
            and exchange.has_open(98.0, "sell")
            and saved_open_order_ids(tmp_path) == _exchange_open_ids(exchange)
        )
    )
    run.process.send_signal(signal.SIGTERM)
    assert run.finish() == 143, run.log.read_text()

    assert [entry["price"] for entry in _created(run, "limit", "sell")][-2:] == [98.0, 94.0]
    assert "is busy" not in run.log.read_text()
    _assert_saved_state_matches_the_exchange(tmp_path, exchange)


def test_a_take_profit_during_a_sigterm_stop_halts_the_bot(tmp_path):
    run = start(tmp_path, "take_profit", overrides=TAKE_PROFIT)
    run.wait_for_grid()
    exchange = ExchangeState(exchange_path(tmp_path))
    grid_orders = _exchange_open_ids(exchange)
    exchange.set_scenario(cancel_delay=1.0, price=106.0)
    run.wait_for(lambda: "cancel_order" in methods(run))

    run.process.send_signal(signal.SIGTERM)

    assert run.finish(timeout=60) == 0, run.log.read_text()
    assert len(halt_markers(tmp_path)) == 1
    assert {entry["id"] for entry in run.entries() if entry["method"] == "cancel_order"} == grid_orders
    assert [entry["side"] for entry in _created(run, "market")] == ["buy", "sell"]
    assert exchange.totals()["BTC"] == pytest.approx(0.0, abs=1e-6)
