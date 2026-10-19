from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from tests.fakes.fake_exchange import ExchangeState

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).with_name("lifecycle_harness.py")
NUM_GRIDS = 4

_runs: list["Run"] = []


@pytest.fixture(autouse=True)
def _kill_leftover_bots():
    yield
    while _runs:
        process = _runs.pop().process
        if process.poll() is None:
            process.kill()
            process.wait()


def _config(tmp_path):
    return {
        "exchange": {"name": "binance", "trading_fee": 0.001, "trading_mode": "live"},
        "pair": {"base_currency": "BTC", "quote_currency": "USDT"},
        "trading_settings": {
            "initial_balance": 1000,
            "timeframe": "1m",
            "period": {"start_date": "2024-07-04T00:00:00Z", "end_date": "2024-07-11T00:00:00Z"},
        },
        "grid_strategy": {
            "type": "simple_grid",
            "spacing": "arithmetic",
            "num_grids": NUM_GRIDS,
            "range": {"top": 110, "bottom": 90},
        },
        "risk_management": {
            "take_profit": {"enabled": False, "threshold": 200},
            "stop_loss": {"enabled": False, "threshold": 50},
        },
        "persistence": {"enabled": True, "db_path": str(tmp_path / "data" / "state.db")},
        "execution": {
            "order_polling_interval": 1.0,
            "websocket_max_retries": 1,
            "websocket_retry_base_delay": 1,
            "health_check_interval": 3600,
            "reconciliation_interval": 3600,
        },
        "logging": {"log_level": "INFO", "log_to_file": False},
    }


class Run:
    def __init__(self, tmp_path, name, args, stdin=None):
        self.calls = tmp_path / f"{name}_calls.jsonl"
        self.log = tmp_path / f"{name}.log"
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)]),
            "EXCHANGE_API_KEY": "test",
            "EXCHANGE_SECRET_KEY": "test",
            "APPRISE_NOTIFICATION_URLS": "",
        }
        with self.log.open("w") as log:
            self.process = subprocess.Popen(  # noqa: S603
                [sys.executable, str(HARNESS), str(_exchange_path(tmp_path)), str(self.calls), *args],
                cwd=tmp_path,
                env=env,
                stdin=stdin if stdin is not None else subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        _runs.append(self)

    def entries(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def limit_order_ids(self):
        return {e["id"] for e in self.entries() if e["method"] == "create_order" and e["type"] == "limit"}

    def open_order_ids(self):
        cancelled = {e["id"] for e in self.entries() if e["method"] == "cancel_order"}
        return self.limit_order_ids() - cancelled

    def wait_for_limit_orders(self, count, timeout=30.0):
        """Waits until at least `count` limit orders were placed."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.limit_order_ids()) >= count:
                return
            if self.process.poll() is not None:
                raise AssertionError(f"bot exited early:\n{self.log.read_text()}")
            time.sleep(0.02)
        raise AssertionError(f"{count} limit orders never placed:\n{self.log.read_text()}")

    def wait_for_grid(self, timeout=30.0):
        """Waits until every grid level holds a limit order."""
        self.wait_for_limit_orders(NUM_GRIDS, timeout)

    def wait_for(self, condition, timeout=30.0):
        """Waits until `condition()` holds while the bot runs."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            if self.process.poll() is not None:
                raise AssertionError(f"bot exited early:\n{self.log.read_text()}")
            time.sleep(0.02)
        raise AssertionError(f"condition never held:\n{self.log.read_text()}")

    def finish(self, timeout=30.0):
        return self.process.wait(timeout=timeout)


def _exchange_path(tmp_path):
    return tmp_path / "exchange.json"


def _start(tmp_path, name, scenario=None, stdin=None):
    if not _exchange_path(tmp_path).exists():
        ExchangeState.create(_exchange_path(tmp_path), **(scenario or {}))
    config_path = tmp_path / "config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps(_config(tmp_path)))
    return Run(tmp_path, name, ["run", "--config", str(config_path), "--no-plot"], stdin=stdin)


def _methods(run):
    return [e["method"] for e in run.entries()]


def _halt_markers(tmp_path):
    return list((tmp_path / "data").glob("halted_*.json"))


def _saved_open_order_ids(tmp_path):
    database_uri = f"{(tmp_path / 'data' / 'state.db').as_uri()}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as database:
        return {row[0] for row in database.execute("SELECT identifier FROM orders WHERE status = 'open'")}


def _saved_state(tmp_path):
    database_uri = f"{(tmp_path / 'data' / 'state.db').as_uri()}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as database:
        fiat, reserved_fiat, crypto, reserved_crypto = database.execute(
            "SELECT fiat_balance, reserved_fiat, crypto_balance, reserved_crypto FROM balance_state"
        ).fetchone()
        filled = dict(database.execute("SELECT identifier, filled FROM orders"))
    return SimpleNamespace(
        fiat_total=float(fiat) + float(reserved_fiat),
        crypto_total=float(crypto) + float(reserved_crypto),
        filled=filled,
    )


def test_sigterm_keeps_the_orders_and_exits_143(tmp_path):
    run = _start(tmp_path, "sigterm")
    run.wait_for_grid()

    run.process.send_signal(signal.SIGTERM)

    assert run.finish() == 143, run.log.read_text()
    methods = _methods(run)
    assert "cancel_order" not in methods
    assert methods.count("close") == 1
    assert methods[-1] == "close"
    assert _saved_open_order_ids(tmp_path) == run.limit_order_ids()
    assert _halt_markers(tmp_path) == []


def test_sigterm_while_the_grid_is_placed_waits_for_it_and_saves_every_order(tmp_path):
    run = _start(tmp_path, "arming", scenario={"order_delay": 0.3})
    run.wait_for_limit_orders(1)
    assert len(run.limit_order_ids()) < NUM_GRIDS

    run.process.send_signal(signal.SIGTERM)

    assert run.finish() == 143, run.log.read_text()
    methods = _methods(run)
    assert "cancel_order" not in methods
    assert "create_order" not in methods[methods.index("close") :]
    assert _saved_open_order_ids(tmp_path) == run.open_order_ids()


def test_a_lost_feed_keeps_the_orders_and_exits_75(tmp_path):
    run = _start(tmp_path, "feed", scenario={"fail_feed_after": 3})

    assert run.finish() == 75, run.log.read_text()
    methods = _methods(run)
    assert "create_order" in methods
    assert "cancel_order" not in methods
    assert methods.count("close") == 1


def test_halted_config_refuses_and_resume_restarts_it(tmp_path):
    first = _start(tmp_path, "halt")
    first.wait_for_grid()
    open_limits = first.limit_order_ids()

    first.process.send_signal(signal.SIGUSR1)

    assert first.finish() == 0, first.log.read_text()
    assert {e["id"] for e in first.entries() if e["method"] == "cancel_order"} == open_limits
    assert len(_halt_markers(tmp_path)) == 1

    refused = _start(tmp_path, "refused")
    assert refused.finish() == 0, refused.log.read_text()
    assert refused.entries() == []

    config_path = tmp_path / "config.json"
    resume = Run(tmp_path, "resume", ["resume", "--config", str(config_path)])
    assert resume.finish() == 0, resume.log.read_text()
    assert _halt_markers(tmp_path) == []

    third = _start(tmp_path, "third")
    third.wait_for_grid()
    third.process.send_signal(signal.SIGTERM)
    assert third.finish() == 143, third.log.read_text()


def test_stdin_on_dev_null_neither_spins_nor_blocks_the_exit(tmp_path):
    run = _start(tmp_path, "devnull", stdin=subprocess.DEVNULL)
    run.wait_for_grid()

    run.process.send_signal(signal.SIGTERM)

    assert run.finish() == 143, run.log.read_text()
    assert len(run.log.read_text().splitlines()) < 1500


def test_a_restart_after_sigterm_resumes_the_kept_orders(tmp_path):
    first = _start(tmp_path, "first")
    first.wait_for_grid()
    first.process.send_signal(signal.SIGTERM)
    assert first.finish() == 143, first.log.read_text()

    exchange = ExchangeState(_exchange_path(tmp_path))
    buys = sorted(exchange.open_orders("buy"), key=lambda order: order["price"])
    exchange.fill(buys[-1]["id"])
    exchange.fill(buys[0]["id"], buys[0]["amount"] * 0.4)

    second = _start(tmp_path, "second")
    second.wait_for(lambda: len(exchange.open_orders("sell")) == 3)
    second.process.send_signal(signal.SIGTERM)
    assert second.finish() == 143, second.log.read_text()

    created = [entry for entry in second.entries() if entry["method"] == "create_order"]
    assert [(entry["type"], entry["side"]) for entry in created] == [("limit", "sell")]
    assert _saved_open_order_ids(tmp_path) == {order["id"] for order in exchange.open_orders()}
    saved = _saved_state(tmp_path)
    assert saved.filled[buys[0]["id"]] == pytest.approx(buys[0]["amount"] * 0.4)
    totals = exchange.totals()
    assert saved.fiat_total == pytest.approx(totals["USDT"], abs=1e-6)
    assert saved.crypto_total == pytest.approx(totals["BTC"], abs=1e-6)


def test_a_fresh_start_over_the_kept_orders_is_refused(tmp_path):
    first = _start(tmp_path, "first")
    first.wait_for_grid()
    first.process.send_signal(signal.SIGTERM)
    assert first.finish() == 143, first.log.read_text()
    for database_file in (tmp_path / "data").glob("state.db*"):
        database_file.unlink()

    second = _start(tmp_path, "second")

    assert second.finish() == 0, second.log.read_text()
    assert "create_order" not in _methods(second)
    assert "still open" in second.log.read_text()
