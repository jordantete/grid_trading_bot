from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

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
    def __init__(self, tmp_path, name, scenario, args, stdin=None):
        self.calls = tmp_path / f"{name}_calls.jsonl"
        self.log = tmp_path / f"{name}.log"
        scenario_path = tmp_path / f"{name}_scenario.json"
        scenario_path.write_text(json.dumps(scenario))
        env = {
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "EXCHANGE_API_KEY": "test",
            "EXCHANGE_SECRET_KEY": "test",
            "APPRISE_NOTIFICATION_URLS": "",
        }
        with self.log.open("w") as log:
            self.process = subprocess.Popen(  # noqa: S603
                [sys.executable, str(HARNESS), str(scenario_path), str(self.calls), *args],
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

    def wait_for_grid(self, timeout=30.0):
        """Waits until every grid level holds a limit order."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.limit_order_ids()) >= NUM_GRIDS:
                return
            if self.process.poll() is not None:
                raise AssertionError(f"bot exited early:\n{self.log.read_text()}")
            time.sleep(0.1)
        raise AssertionError(f"grid never placed:\n{self.log.read_text()}")

    def finish(self, timeout=30.0):
        return self.process.wait(timeout=timeout)


def _start(tmp_path, name, scenario=None, stdin=None):
    config_path = tmp_path / "config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps(_config(tmp_path)))
    return Run(tmp_path, name, scenario or {}, ["run", "--config", str(config_path), "--no-plot"], stdin=stdin)


def _methods(run):
    return [e["method"] for e in run.entries()]


def _halt_markers(tmp_path):
    return list((tmp_path / "data").glob("halted_*.json"))


def _saved_open_order_ids(tmp_path):
    database_uri = f"{(tmp_path / 'data' / 'state.db').as_uri()}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as database:
        return {row[0] for row in database.execute("SELECT identifier FROM orders WHERE status = 'open'")}


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
    resume = Run(tmp_path, "resume", {}, ["resume", "--config", str(config_path)])
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
