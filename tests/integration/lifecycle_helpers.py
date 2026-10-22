"""Runs the grid_trading_bot CLI in a subprocess on the JSON-persisted fake exchange, and reads what it left behind."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

from tests.fakes.fake_exchange import ExchangeState

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).with_name("lifecycle_harness.py")
NUM_GRIDS = 4

_runs: list["Run"] = []


def kill_leftover_runs() -> None:
    """Kills the bot processes a failed test left running."""
    while _runs:
        process = _runs.pop().process
        if process.poll() is None:
            process.kill()
            process.wait()


def base_config(tmp_path) -> dict:
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


def _merged(base: dict, overrides: dict) -> dict:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = _merged(base[key], value)
        else:
            merged[key] = value
    return merged


class Run:
    def __init__(self, tmp_path, name, args, stdin=None, env=None):
        self.calls = tmp_path / f"{name}_calls.jsonl"
        self.log = tmp_path / f"{name}.log"
        process_env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)]),
            "EXCHANGE_API_KEY": "test",
            "EXCHANGE_SECRET_KEY": "test",
            "APPRISE_NOTIFICATION_URLS": "",
            **(env or {}),
        }
        with self.log.open("w") as log:
            self.process = subprocess.Popen(  # noqa: S603
                [sys.executable, str(HARNESS), str(exchange_path(tmp_path)), str(self.calls), *args],
                cwd=tmp_path,
                env=process_env,
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


def exchange_path(tmp_path):
    return tmp_path / "exchange.json"


def start(tmp_path, name, scenario=None, stdin=None, overrides=None, env=None):
    """Starts a bot on the test's exchange and config, creating both on the first start of the test."""
    if not exchange_path(tmp_path).exists():
        ExchangeState.create(exchange_path(tmp_path), **(scenario or {}))
    config_path = tmp_path / "config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps(_merged(base_config(tmp_path), overrides or {})))
    return Run(tmp_path, name, ["run", "--config", str(config_path), "--no-plot"], stdin=stdin, env=env)


def methods(run):
    return [e["method"] for e in run.entries()]


def halt_markers(tmp_path):
    return list((tmp_path / "data").glob("halted_*.json"))


def _database(tmp_path):
    database_uri = f"{(tmp_path / 'data' / 'state.db').as_uri()}?mode=ro"
    return closing(sqlite3.connect(database_uri, uri=True))


def saved_open_order_ids(tmp_path):
    with _database(tmp_path) as database:
        return {row[0] for row in database.execute("SELECT identifier FROM orders WHERE status = 'open'")}


def saved_state(tmp_path):
    with _database(tmp_path) as database:
        fiat, reserved_fiat, crypto, reserved_crypto = database.execute(
            "SELECT fiat_balance, reserved_fiat, crypto_balance, reserved_crypto FROM balance_state"
        ).fetchone()
        filled = dict(database.execute("SELECT identifier, filled FROM orders"))
    return SimpleNamespace(
        fiat_total=float(fiat) + float(reserved_fiat),
        crypto_total=float(crypto) + float(reserved_crypto),
        filled=filled,
    )


def saved_pending(tmp_path) -> list[dict]:
    with _database(tmp_path) as database:
        database.row_factory = sqlite3.Row
        return [dict(row) for row in database.execute("SELECT * FROM pending_placements ORDER BY created_at")]


def reserved_gap(tmp_path) -> tuple[float, float]:
    """The saved reserved fiat and crypto minus what the saved open orders and limit entries hold."""
    with _database(tmp_path) as database:
        reserved_fiat, reserved_crypto = database.execute(
            "SELECT reserved_fiat, reserved_crypto FROM balance_state"
        ).fetchone()
        held = [
            *database.execute("SELECT side, price, remaining FROM orders WHERE status = 'open'"),
            *database.execute("SELECT side, price, quantity FROM pending_placements WHERE order_type = 'limit'"),
        ]
    fiat = sum(price * quantity for side, price, quantity in held if side == "buy")
    crypto = sum(quantity for side, _, quantity in held if side == "sell")
    return float(reserved_fiat) - fiat, float(reserved_crypto) - crypto
