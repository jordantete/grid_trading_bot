import signal
import subprocess

import pytest

from tests.fakes.fake_exchange import ExchangeState
from tests.integration.lifecycle_helpers import (
    NUM_GRIDS,
    Run,
    exchange_path,
    halt_markers,
    methods,
    saved_open_order_ids,
    saved_state,
    start,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def test_sigterm_keeps_the_orders_and_exits_143(tmp_path):
    run = start(tmp_path, "sigterm")
    run.wait_for_grid()

    run.process.send_signal(signal.SIGTERM)

    assert run.finish() == 143, run.log.read_text()
    calls = methods(run)
    assert "cancel_order" not in calls
    assert calls.count("close") == 1
    assert calls[-1] == "close"
    assert saved_open_order_ids(tmp_path) == run.limit_order_ids()
    assert halt_markers(tmp_path) == []


def test_sigterm_while_the_grid_is_placed_waits_for_it_and_saves_every_order(tmp_path):
    run = start(tmp_path, "arming", scenario={"order_delay": 0.3})
    run.wait_for_limit_orders(1)
    assert len(run.limit_order_ids()) < NUM_GRIDS

    run.process.send_signal(signal.SIGTERM)

    assert run.finish() == 143, run.log.read_text()
    calls = methods(run)
    assert "cancel_order" not in calls
    assert "create_order" not in calls[calls.index("close") :]
    assert saved_open_order_ids(tmp_path) == run.open_order_ids()


def test_a_lost_feed_keeps_the_orders_and_exits_75(tmp_path):
    run = start(tmp_path, "feed", scenario={"fail_feed_after": 3})

    assert run.finish() == 75, run.log.read_text()
    calls = methods(run)
    assert "create_order" in calls
    assert "cancel_order" not in calls
    assert calls.count("close") == 1


def test_three_failed_polls_in_a_row_stop_the_bot_with_its_orders_kept_and_exit_1(tmp_path):
    run = start(tmp_path, "loop", env={"TRACKER_POLLS_BEFORE_FAILING": "3"})
    run.wait_for_grid()

    assert run.finish() == 1, run.log.read_text()
    calls = methods(run)
    assert "cancel_order" not in calls
    assert calls.count("close") == 1
    assert saved_open_order_ids(tmp_path) == run.limit_order_ids()
    assert halt_markers(tmp_path) == []
    assert "Order tracking failed 3 times in a row" in run.log.read_text()


def test_halted_config_refuses_and_resume_restarts_it(tmp_path):
    first = start(tmp_path, "halt")
    first.wait_for_grid()
    open_limits = first.limit_order_ids()

    first.process.send_signal(signal.SIGUSR1)

    assert first.finish() == 0, first.log.read_text()
    assert {e["id"] for e in first.entries() if e["method"] == "cancel_order"} == open_limits
    assert len(halt_markers(tmp_path)) == 1

    refused = start(tmp_path, "refused")
    assert refused.finish() == 0, refused.log.read_text()
    assert refused.entries() == []

    config_path = tmp_path / "config.json"
    resume = Run(tmp_path, "resume", ["resume", "--config", str(config_path)])
    assert resume.finish() == 0, resume.log.read_text()
    assert halt_markers(tmp_path) == []

    third = start(tmp_path, "third")
    third.wait_for_grid()
    third.process.send_signal(signal.SIGTERM)
    assert third.finish() == 143, third.log.read_text()


def test_stdin_on_dev_null_neither_spins_nor_blocks_the_exit(tmp_path):
    run = start(tmp_path, "devnull", stdin=subprocess.DEVNULL)
    run.wait_for_grid()

    run.process.send_signal(signal.SIGTERM)

    assert run.finish() == 143, run.log.read_text()
    assert len(run.log.read_text().splitlines()) < 1500


def test_a_restart_after_sigterm_resumes_the_kept_orders(tmp_path):
    first = start(tmp_path, "first")
    first.wait_for_grid()
    first.process.send_signal(signal.SIGTERM)
    assert first.finish() == 143, first.log.read_text()

    exchange = ExchangeState(exchange_path(tmp_path))
    buys = sorted(exchange.open_orders("buy"), key=lambda order: order["price"])
    exchange.fill(buys[-1]["id"])
    exchange.fill(buys[0]["id"], buys[0]["amount"] * 0.4)

    second = start(tmp_path, "second")
    second.wait_for(lambda: len(exchange.open_orders("sell")) == 3)
    second.process.send_signal(signal.SIGTERM)
    assert second.finish() == 143, second.log.read_text()

    created = [entry for entry in second.entries() if entry["method"] == "create_order"]
    assert [(entry["type"], entry["side"]) for entry in created] == [("limit", "sell")]
    assert saved_open_order_ids(tmp_path) == {order["id"] for order in exchange.open_orders()}
    saved = saved_state(tmp_path)
    assert saved.filled[buys[0]["id"]] == pytest.approx(buys[0]["amount"] * 0.4)
    totals = exchange.totals()
    assert saved.fiat_total == pytest.approx(totals["USDT"], abs=1e-6)
    assert saved.crypto_total == pytest.approx(totals["BTC"], abs=1e-6)


def test_a_fresh_start_over_the_kept_orders_is_refused(tmp_path):
    first = start(tmp_path, "first")
    first.wait_for_grid()
    first.process.send_signal(signal.SIGTERM)
    assert first.finish() == 143, first.log.read_text()
    for database_file in (tmp_path / "data").glob("state.db*"):
        database_file.unlink()

    second = start(tmp_path, "second")

    assert second.finish() == 0, second.log.read_text()
    assert "create_order" not in methods(second)
    assert "still open" in second.log.read_text()
