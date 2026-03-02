import os
import tempfile

import pytest

from grid_trading_bot.core.persistence.sqlite_state_repository import SQLiteStateRepository


@pytest.fixture
def repo():
    """Create an in-memory SQLiteStateRepository, initialize it, and close after test."""
    repository = SQLiteStateRepository(db_path=":memory:")
    repository.initialize()
    yield repository
    repository.close()


@pytest.fixture
def sample_order():
    return {
        "identifier": "order-1",
        "status": "open",
        "order_type": "limit",
        "side": "buy",
        "price": 100.0,
        "average": None,
        "amount": 1.0,
        "filled": 0.0,
        "remaining": 1.0,
        "timestamp": 1700000000,
        "datetime_str": "2023-11-14T00:00:00Z",
        "last_trade_timestamp": None,
        "symbol": "ETH/USDT",
        "time_in_force": "GTC",
        "cost": None,
        "trades_json": None,
        "fee_json": None,
        "info_json": None,
        "grid_level_price": 100.0,
        "is_non_grid_order": 0,
    }


@pytest.fixture
def sample_bot_state():
    return {
        "config_hash": "abc123",
        "trading_pair": "ETH/USDT",
        "strategy_type": "simple_grid",
        "initial_purchase_done": True,
        "grid_orders_initialized": False,
    }


@pytest.fixture
def sample_balance_state():
    return {
        "fiat_balance": "5000.00",
        "crypto_balance": "2.50000000",
        "total_fees": "12.34",
        "reserved_fiat": "1000.00",
        "reserved_crypto": "0.50000000",
    }


@pytest.fixture
def sample_grid_level():
    return {
        "price": 2950.0,
        "state": "READY_TO_BUY",
        "paired_buy_level_price": None,
        "paired_sell_level_price": 3050.0,
    }


class TestInitialize:
    def test_initialize_creates_tables(self, repo):
        cursor = repo._conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row["name"] for row in cursor.fetchall()}
        expected_tables = {"schema_version", "bot_state", "balance_state", "orders", "grid_levels"}
        assert expected_tables.issubset(tables)

    def test_wal_mode_enabled(self):
        """WAL mode requires a file-based database; :memory: reports 'memory'."""
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            file_repo = SQLiteStateRepository(db_path=db_path)
            file_repo.initialize()
            cursor = file_repo._conn.execute("PRAGMA journal_mode")
            mode = cursor.fetchone()[0]
            assert mode == "wal"
            file_repo.close()
        finally:
            os.unlink(db_path)
            # WAL and SHM sidecar files
            for suffix in ("-wal", "-shm"):
                path = db_path + suffix
                if os.path.exists(path):
                    os.unlink(path)

    def test_schema_version_set(self, repo):
        cursor = repo._conn.execute("SELECT version FROM schema_version")
        row = cursor.fetchone()
        assert row is not None
        assert row["version"] == 1


class TestBotState:
    def test_bot_state_round_trip(self, repo, sample_bot_state):
        repo.save_bot_state(sample_bot_state)
        loaded = repo.load_bot_state()

        assert loaded is not None
        assert loaded["config_hash"] == "abc123"
        assert loaded["trading_pair"] == "ETH/USDT"
        assert loaded["strategy_type"] == "simple_grid"
        assert loaded["initial_purchase_done"] is True
        assert loaded["grid_orders_initialized"] is False
        assert "updated_at" in loaded

    def test_bot_state_returns_none_when_empty(self, repo):
        result = repo.load_bot_state()
        assert result is None

    def test_bot_state_upsert(self, repo, sample_bot_state):
        repo.save_bot_state(sample_bot_state)

        updated_state = {
            "config_hash": "def456",
            "trading_pair": "BTC/USDT",
            "strategy_type": "hedged_grid",
            "initial_purchase_done": False,
            "grid_orders_initialized": True,
        }
        repo.save_bot_state(updated_state)

        loaded = repo.load_bot_state()
        assert loaded is not None
        assert loaded["config_hash"] == "def456"
        assert loaded["trading_pair"] == "BTC/USDT"
        assert loaded["strategy_type"] == "hedged_grid"
        assert loaded["initial_purchase_done"] is False
        assert loaded["grid_orders_initialized"] is True


class TestBalanceState:
    def test_balance_state_round_trip(self, repo, sample_balance_state):
        repo.save_balance_state(sample_balance_state)
        loaded = repo.load_balance_state()

        assert loaded is not None
        assert loaded["fiat_balance"] == "5000.00"
        assert loaded["crypto_balance"] == "2.50000000"
        assert loaded["total_fees"] == "12.34"
        assert loaded["reserved_fiat"] == "1000.00"
        assert loaded["reserved_crypto"] == "0.50000000"
        assert "updated_at" in loaded

    def test_balance_state_returns_none_when_empty(self, repo):
        result = repo.load_balance_state()
        assert result is None


class TestOrders:
    def test_order_round_trip(self, repo, sample_order):
        repo.save_order(sample_order)
        orders = repo.load_all_orders()

        assert len(orders) == 1
        order = orders[0]
        assert order["identifier"] == "order-1"
        assert order["status"] == "open"
        assert order["order_type"] == "limit"
        assert order["side"] == "buy"
        assert order["price"] == 100.0
        assert order["average"] is None
        assert order["amount"] == 1.0
        assert order["filled"] == 0.0
        assert order["remaining"] == 1.0
        assert order["timestamp"] == 1700000000
        assert order["datetime_str"] == "2023-11-14T00:00:00Z"
        assert order["last_trade_timestamp"] is None
        assert order["symbol"] == "ETH/USDT"
        assert order["time_in_force"] == "GTC"
        assert order["cost"] is None
        assert order["trades_json"] is None
        assert order["fee_json"] is None
        assert order["info_json"] is None
        assert order["grid_level_price"] == 100.0
        assert order["is_non_grid_order"] == 0

    def test_save_multiple_orders(self, repo, sample_order):
        order_2 = sample_order.copy()
        order_2["identifier"] = "order-2"
        order_2["side"] = "sell"
        order_2["price"] = 110.0

        order_3 = sample_order.copy()
        order_3["identifier"] = "order-3"
        order_3["price"] = 90.0

        repo.save_orders([sample_order, order_2, order_3])
        orders = repo.load_all_orders()

        assert len(orders) == 3
        identifiers = {o["identifier"] for o in orders}
        assert identifiers == {"order-1", "order-2", "order-3"}

    def test_order_upsert(self, repo, sample_order):
        repo.save_order(sample_order)

        updated_order = sample_order.copy()
        updated_order["status"] = "closed"
        updated_order["filled"] = 1.0
        updated_order["remaining"] = 0.0
        repo.save_order(updated_order)

        orders = repo.load_all_orders()
        assert len(orders) == 1
        assert orders[0]["status"] == "closed"
        assert orders[0]["filled"] == 1.0
        assert orders[0]["remaining"] == 0.0


class TestGridLevels:
    def test_grid_level_round_trip(self, repo, sample_grid_level):
        repo.save_grid_level(sample_grid_level)
        levels = repo.load_grid_levels()

        assert len(levels) == 1
        level = levels[0]
        assert level["price"] == 2950.0
        assert level["state"] == "READY_TO_BUY"
        assert level["paired_buy_level_price"] is None
        assert level["paired_sell_level_price"] == 3050.0
        assert "updated_at" in level

    def test_save_multiple_grid_levels(self, repo):
        levels = [
            {
                "price": 2900.0,
                "state": "READY_TO_BUY",
                "paired_buy_level_price": None,
                "paired_sell_level_price": 3000.0,
            },
            {
                "price": 2950.0,
                "state": "READY_TO_SELL",
                "paired_buy_level_price": 2850.0,
                "paired_sell_level_price": None,
            },
            {
                "price": 3000.0,
                "state": "READY_TO_BUY",
                "paired_buy_level_price": None,
                "paired_sell_level_price": 3100.0,
            },
        ]
        repo.save_grid_levels(levels)
        loaded = repo.load_grid_levels()

        assert len(loaded) == 3
        prices = {gl["price"] for gl in loaded}
        assert prices == {2900.0, 2950.0, 3000.0}

    def test_grid_level_upsert(self, repo, sample_grid_level):
        repo.save_grid_level(sample_grid_level)

        updated_level = sample_grid_level.copy()
        updated_level["state"] = "WAITING_FOR_BUY_FILL"
        repo.save_grid_level(updated_level)

        levels = repo.load_grid_levels()
        assert len(levels) == 1
        assert levels[0]["state"] == "WAITING_FOR_BUY_FILL"


class TestClearAll:
    def test_clear_all(self, repo, sample_bot_state, sample_balance_state, sample_order, sample_grid_level):
        repo.save_bot_state(sample_bot_state)
        repo.save_balance_state(sample_balance_state)
        repo.save_order(sample_order)
        repo.save_grid_level(sample_grid_level)

        # Verify data exists before clearing
        assert repo.load_bot_state() is not None
        assert repo.load_balance_state() is not None
        assert len(repo.load_all_orders()) == 1
        assert len(repo.load_grid_levels()) == 1

        repo.clear_all()

        assert repo.load_bot_state() is None
        assert repo.load_balance_state() is None
        assert repo.load_all_orders() == []
        assert repo.load_grid_levels() == []


class TestDecimalPrecision:
    def test_decimal_precision_preserved(self, repo):
        balance_state = {
            "fiat_balance": "1234.12345678",
            "crypto_balance": "0.00000001",
            "total_fees": "99999999.99999999",
            "reserved_fiat": "0.00000000",
            "reserved_crypto": "123456789.12345678",
        }
        repo.save_balance_state(balance_state)
        loaded = repo.load_balance_state()

        assert loaded is not None
        assert loaded["fiat_balance"] == "1234.12345678"
        assert loaded["crypto_balance"] == "0.00000001"
        assert loaded["total_fees"] == "99999999.99999999"
        assert loaded["reserved_fiat"] == "0.00000000"
        assert loaded["reserved_crypto"] == "123456789.12345678"
