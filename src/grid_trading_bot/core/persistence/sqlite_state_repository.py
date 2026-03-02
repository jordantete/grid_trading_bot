import logging
import sqlite3
import threading
from typing import Any

from .state_repository_interface import StateRepositoryInterface

SCHEMA_VERSION = 1

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bot_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    config_hash TEXT NOT NULL,
    trading_pair TEXT NOT NULL,
    strategy_type TEXT NOT NULL,
    initial_purchase_done INTEGER NOT NULL DEFAULT 0,
    grid_orders_initialized INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS balance_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fiat_balance TEXT NOT NULL,
    crypto_balance TEXT NOT NULL,
    total_fees TEXT NOT NULL,
    reserved_fiat TEXT NOT NULL,
    reserved_crypto TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS orders (
    identifier TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    order_type TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    average REAL,
    amount REAL NOT NULL,
    filled REAL NOT NULL DEFAULT 0.0,
    remaining REAL NOT NULL,
    timestamp INTEGER NOT NULL,
    datetime_str TEXT,
    last_trade_timestamp INTEGER,
    symbol TEXT NOT NULL,
    time_in_force TEXT,
    cost REAL,
    trades_json TEXT,
    fee_json TEXT,
    info_json TEXT,
    grid_level_price REAL,
    is_non_grid_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS grid_levels (
    price REAL PRIMARY KEY,
    state TEXT NOT NULL,
    paired_buy_level_price REAL,
    paired_sell_level_price REAL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_grid_level ON orders(grid_level_price);
"""


class SQLiteStateRepository(StateRepositoryInterface):
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.logger = logging.getLogger(self.__class__.__name__)
        self._conn: sqlite3.Connection | None = None
        self._write_lock = threading.Lock()

    def initialize(self) -> None:
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._write_lock:
            self._conn.executescript(_CREATE_TABLES_SQL)
            self._ensure_schema_version()
        self.logger.info(f"SQLiteStateRepository initialized at {self.db_path}")

    def _ensure_schema_version(self) -> None:
        cursor = self._conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            self._conn.commit()
        else:
            stored_version = row["version"]
            if stored_version != SCHEMA_VERSION:
                self.logger.warning(
                    f"Schema version mismatch: stored={stored_version}, expected={SCHEMA_VERSION}. "
                    f"Migration may be needed."
                )

    # ── Bot State ────────────────────────────────────────────────────────

    def save_bot_state(self, state: dict[str, Any]) -> None:
        with self._write_lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO bot_state
                   (id, config_hash, trading_pair, strategy_type,
                    initial_purchase_done, grid_orders_initialized, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, datetime('now'))""",
                (
                    state["config_hash"],
                    state["trading_pair"],
                    state["strategy_type"],
                    1 if state.get("initial_purchase_done") else 0,
                    1 if state.get("grid_orders_initialized") else 0,
                ),
            )
            self._conn.commit()

    def load_bot_state(self) -> dict[str, Any] | None:
        cursor = self._conn.execute("SELECT * FROM bot_state WHERE id = 1")
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "config_hash": row["config_hash"],
            "trading_pair": row["trading_pair"],
            "strategy_type": row["strategy_type"],
            "initial_purchase_done": bool(row["initial_purchase_done"]),
            "grid_orders_initialized": bool(row["grid_orders_initialized"]),
            "updated_at": row["updated_at"],
        }

    # ── Balance State ────────────────────────────────────────────────────

    def save_balance_state(self, state: dict[str, str]) -> None:
        with self._write_lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO balance_state
                   (id, fiat_balance, crypto_balance, total_fees,
                    reserved_fiat, reserved_crypto, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, datetime('now'))""",
                (
                    state["fiat_balance"],
                    state["crypto_balance"],
                    state["total_fees"],
                    state["reserved_fiat"],
                    state["reserved_crypto"],
                ),
            )
            self._conn.commit()

    def load_balance_state(self) -> dict[str, str] | None:
        cursor = self._conn.execute("SELECT * FROM balance_state WHERE id = 1")
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "fiat_balance": row["fiat_balance"],
            "crypto_balance": row["crypto_balance"],
            "total_fees": row["total_fees"],
            "reserved_fiat": row["reserved_fiat"],
            "reserved_crypto": row["reserved_crypto"],
            "updated_at": row["updated_at"],
        }

    # ── Orders ───────────────────────────────────────────────────────────

    def save_order(self, order_dict: dict[str, Any]) -> None:
        with self._write_lock:
            self._upsert_order(order_dict)
            self._conn.commit()

    def save_orders(self, order_dicts: list[dict[str, Any]]) -> None:
        with self._write_lock:
            for order_dict in order_dicts:
                self._upsert_order(order_dict)
            self._conn.commit()

    def _upsert_order(self, d: dict[str, Any]) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO orders
               (identifier, status, order_type, side, price, average,
                amount, filled, remaining, timestamp, datetime_str,
                last_trade_timestamp, symbol, time_in_force, cost,
                trades_json, fee_json, info_json, grid_level_price,
                is_non_grid_order, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                d["identifier"],
                d["status"],
                d["order_type"],
                d["side"],
                d["price"],
                d.get("average"),
                d["amount"],
                d.get("filled", 0.0),
                d["remaining"],
                d["timestamp"],
                d.get("datetime_str"),
                d.get("last_trade_timestamp"),
                d["symbol"],
                d.get("time_in_force"),
                d.get("cost"),
                d.get("trades_json"),
                d.get("fee_json"),
                d.get("info_json"),
                d.get("grid_level_price"),
                d.get("is_non_grid_order", 0),
            ),
        )

    def load_all_orders(self) -> list[dict[str, Any]]:
        cursor = self._conn.execute("SELECT * FROM orders")
        return [dict(row) for row in cursor.fetchall()]

    # ── Grid Levels ──────────────────────────────────────────────────────

    def save_grid_level(self, grid_level_dict: dict[str, Any]) -> None:
        with self._write_lock:
            self._upsert_grid_level(grid_level_dict)
            self._conn.commit()

    def save_grid_levels(self, grid_level_dicts: list[dict[str, Any]]) -> None:
        with self._write_lock:
            for gl_dict in grid_level_dicts:
                self._upsert_grid_level(gl_dict)
            self._conn.commit()

    def _upsert_grid_level(self, d: dict[str, Any]) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO grid_levels
               (price, state, paired_buy_level_price, paired_sell_level_price, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (
                d["price"],
                d["state"],
                d.get("paired_buy_level_price"),
                d.get("paired_sell_level_price"),
            ),
        )

    def load_grid_levels(self) -> list[dict[str, Any]]:
        cursor = self._conn.execute("SELECT * FROM grid_levels")
        return [dict(row) for row in cursor.fetchall()]

    # ── Utilities ────────────────────────────────────────────────────────

    def clear_all(self) -> None:
        with self._write_lock:
            self._conn.executescript("""
                DELETE FROM grid_levels;
                DELETE FROM orders;
                DELETE FROM balance_state;
                DELETE FROM bot_state;
            """)
        self.logger.info("All persisted state cleared.")

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
            self.logger.info("SQLiteStateRepository connection closed.")
