import logging
import sqlite3
import threading
from typing import Any

from .state_repository_interface import SavedBalance, StateRepositoryInterface, StateSnapshot

SCHEMA_VERSION = 4

_CREATE_PENDING_PLACEMENTS_SQL = """
CREATE TABLE IF NOT EXISTS pending_placements (
    client_order_id TEXT PRIMARY KEY,
    order_type TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL,
    purpose TEXT NOT NULL,
    level_price REAL,
    source_level_price REAL,
    source_order_id TEXT,
    sent INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
"""

_CREATE_TABLES_SQL = (
    """
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
    strategy_state TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS balance_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fiat_balance TEXT NOT NULL,
    crypto_balance TEXT NOT NULL,
    total_fees TEXT NOT NULL,
    reserved_fiat TEXT NOT NULL,
    reserved_crypto TEXT NOT NULL,
    avg_cost TEXT,
    realized_pnl TEXT,
    peak_value TEXT,
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
    + _CREATE_PENDING_PLACEMENTS_SQL
)


class SQLiteStateRepository(StateRepositoryInterface):
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.logger = logging.getLogger(self.__class__.__name__)
        self._conn: sqlite3.Connection | None = None
        self._write_lock = threading.Lock()

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError(f"SQLiteStateRepository at {self.db_path} is not open.")
        return self._conn

    def initialize(self) -> None:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._conn = conn
        with self._write_lock:
            self._db.executescript(_CREATE_TABLES_SQL)
            self._ensure_schema_version()
        self.logger.info(f"SQLiteStateRepository initialized at {self.db_path}")

    def _ensure_schema_version(self) -> None:
        cursor = self._db.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            self._db.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            self._db.commit()
            return

        stored_version = row["version"]
        if stored_version == 1:
            self._migrate_v1_to_v2()
            stored_version = 2
        if stored_version == 2:
            self._migrate_v2_to_v3()
            stored_version = 3
        if stored_version == 3:
            self._migrate_v3_to_v4()
            stored_version = 4

        if stored_version != SCHEMA_VERSION:
            self.logger.warning(
                f"Schema version mismatch: stored={stored_version}, expected={SCHEMA_VERSION}. Migration may be needed."
            )

    def _migrate_v1_to_v2(self) -> None:
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(bot_state)")}
        if "strategy_state" not in columns:
            self._db.execute("ALTER TABLE bot_state ADD COLUMN strategy_state TEXT")
        self._db.execute("INSERT INTO schema_version (version) VALUES (?)", (2,))
        self._db.commit()
        self.logger.info("Migrated schema v1 -> v2 (added bot_state.strategy_state).")

    def _migrate_v2_to_v3(self) -> None:
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(balance_state)")}
        for column in ("avg_cost", "realized_pnl", "peak_value"):
            if column not in columns:
                self._db.execute(f"ALTER TABLE balance_state ADD COLUMN {column} TEXT")
        self._db.execute("INSERT INTO schema_version (version) VALUES (?)", (3,))
        self._db.commit()
        self.logger.info("Migrated schema v2 -> v3 (added balance_state.avg_cost, realized_pnl, peak_value).")

    def _migrate_v3_to_v4(self) -> None:
        self._db.executescript(_CREATE_PENDING_PLACEMENTS_SQL)
        self._db.execute("INSERT INTO schema_version (version) VALUES (?)", (4,))
        self._db.commit()
        self.logger.info("Migrated schema v3 -> v4 (added pending_placements).")

    # ── Bot State ────────────────────────────────────────────────────────

    def save_state(self, snapshot: StateSnapshot) -> None:
        with self._write_lock, self._db:
            self._db.execute("DELETE FROM orders")
            self._db.execute("DELETE FROM grid_levels")
            self._db.execute("DELETE FROM pending_placements")
            self._upsert_bot_state(snapshot.bot_state)
            self._upsert_balance_state(snapshot.balance_state)
            for order_dict in snapshot.orders:
                self._upsert_order(order_dict)
            for grid_level_dict in snapshot.grid_levels:
                self._upsert_grid_level(grid_level_dict)
            for row in snapshot.pending_placements:
                self._upsert_pending(row)

    def _upsert_bot_state(self, state: dict[str, Any]) -> None:
        self._db.execute(
            """INSERT OR REPLACE INTO bot_state
               (id, config_hash, trading_pair, strategy_type,
                initial_purchase_done, grid_orders_initialized, strategy_state, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                state["config_hash"],
                state["trading_pair"],
                state["strategy_type"],
                1 if state.get("initial_purchase_done") else 0,
                1 if state.get("grid_orders_initialized") else 0,
                state.get("strategy_state"),
            ),
        )

    def load_bot_state(self) -> dict[str, Any] | None:
        cursor = self._db.execute("SELECT * FROM bot_state WHERE id = 1")
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "config_hash": row["config_hash"],
            "trading_pair": row["trading_pair"],
            "strategy_type": row["strategy_type"],
            "initial_purchase_done": bool(row["initial_purchase_done"]),
            "grid_orders_initialized": bool(row["grid_orders_initialized"]),
            "strategy_state": row["strategy_state"],
            "updated_at": row["updated_at"],
        }

    # ── Balance State ────────────────────────────────────────────────────

    def _upsert_balance_state(self, state: dict[str, str | None]) -> None:
        self._db.execute(
            """INSERT OR REPLACE INTO balance_state
               (id, fiat_balance, crypto_balance, total_fees,
                reserved_fiat, reserved_crypto, avg_cost, realized_pnl, peak_value, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                state["fiat_balance"],
                state["crypto_balance"],
                state["total_fees"],
                state["reserved_fiat"],
                state["reserved_crypto"],
                state.get("avg_cost"),
                state.get("realized_pnl"),
                state.get("peak_value"),
            ),
        )

    def load_balance_state(self) -> SavedBalance | None:
        cursor = self._db.execute("SELECT * FROM balance_state WHERE id = 1")
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "fiat_balance": row["fiat_balance"],
            "crypto_balance": row["crypto_balance"],
            "total_fees": row["total_fees"],
            "reserved_fiat": row["reserved_fiat"],
            "reserved_crypto": row["reserved_crypto"],
            "avg_cost": row["avg_cost"],
            "realized_pnl": row["realized_pnl"],
            "peak_value": row["peak_value"],
            "updated_at": row["updated_at"],
        }

    # ── Orders ───────────────────────────────────────────────────────────

    def _upsert_order(self, d: dict[str, Any]) -> None:
        self._db.execute(
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
        cursor = self._db.execute("SELECT * FROM orders")
        return [dict(row) for row in cursor.fetchall()]

    # ── Grid Levels ──────────────────────────────────────────────────────

    def _upsert_grid_level(self, d: dict[str, Any]) -> None:
        self._db.execute(
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
        cursor = self._db.execute("SELECT * FROM grid_levels")
        return [dict(row) for row in cursor.fetchall()]

    # ── Pending Placements ───────────────────────────────────────────────

    def _upsert_pending(self, row: dict[str, Any]) -> None:
        self._db.execute(
            """INSERT OR REPLACE INTO pending_placements
               (client_order_id, order_type, side, quantity, price, purpose,
                level_price, source_level_price, source_order_id, sent, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["client_order_id"],
                row["order_type"],
                row["side"],
                row["quantity"],
                row.get("price"),
                row["purpose"],
                row.get("level_price"),
                row.get("source_level_price"),
                row.get("source_order_id"),
                1 if row["sent"] else 0,
                row["created_at"],
            ),
        )

    def save_pending(self, row: dict[str, Any]) -> None:
        with self._write_lock, self._db:
            self._upsert_pending(row)

    def load_pending_placements(self) -> list[dict[str, Any]]:
        cursor = self._db.execute("SELECT * FROM pending_placements ORDER BY created_at, rowid")
        return [{**dict(row), "sent": bool(row["sent"])} for row in cursor.fetchall()]

    # ── Utilities ────────────────────────────────────────────────────────

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
            self.logger.info("SQLiteStateRepository connection closed.")
