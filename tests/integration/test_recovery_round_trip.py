"""Runs whole bots in process against the persistent fake exchange and a real SQLite state database."""

import asyncio
from contextlib import closing
from decimal import Decimal
import json
import signal
import sqlite3
import time
from types import SimpleNamespace

import pytest

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.bot_management.event_bus import EventBus
from grid_trading_bot.core.bot_management.grid_trading_bot import GridTradingBot
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest
from grid_trading_bot.core.order_handling.order import OrderSide, OrderType
from grid_trading_bot.core.order_handling.placement_journal import (
    PendingPlacement,
    PlacementPurpose,
    new_client_order_id,
)
from grid_trading_bot.core.order_handling.pnl_ledger import PnlLedger
from grid_trading_bot.core.persistence.run_files import RunFiles
from grid_trading_bot.core.persistence.serializers import pending_placement_to_dict
from grid_trading_bot.core.persistence.sqlite_state_repository import SQLiteStateRepository
from grid_trading_bot.core.services import live_exchange_service
from grid_trading_bot.strategies.live_grid_strategy import LiveGridStrategy
from tests.fakes.fake_exchange import ExchangeState, FakeExchange

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

# A 5-level grid from 80 to 120 armed at 115: buys rest on 80, 90 and 100, a sell on 120, 110 stays free.
ARMING_PRICE = 115.0
ARMED_ORDERS = 4


@pytest.fixture
def exchange(tmp_path, monkeypatch):
    state = ExchangeState.create(tmp_path / "exchange.json", price=ARMING_PRICE, quote=1000.0)
    monkeypatch.setattr(FakeExchange, "state_path", state.path)
    monkeypatch.setattr(live_exchange_service, "ccxtpro", SimpleNamespace(binance=FakeExchange))
    monkeypatch.setattr(LiveGridStrategy, "TICKER_REFRESH_INTERVAL", 0.05)
    monkeypatch.setenv("EXCHANGE_API_KEY", "test")
    monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test")
    monkeypatch.setenv("APPRISE_NOTIFICATION_URLS", "")
    return state


class SaveChecks:
    def __init__(self):
        self.saves = 0
        self.problems: list[str] = []


@pytest.fixture
def checks(monkeypatch):
    """Checks every snapshot the bots save. Problems are recorded rather than raised: an exception raised inside the
    save would be taken for a failed checkpoint."""
    recorded = SaveChecks()
    save_state = SQLiteStateRepository.save_state

    def checked_save_state(self, snapshot):
        recorded.saves += 1
        recorded.problems.extend(_violations(snapshot))
        save_state(self, snapshot)

    monkeypatch.setattr(SQLiteStateRepository, "save_state", checked_save_state)
    return recorded


def _violations(snapshot) -> list[str]:
    """
    What breaks the saved-state invariants: reserved funds equal to what the open orders need, and each waiting level
    holding an open order.
    """
    open_orders = [o for o in snapshot.orders if o["status"] == "open"]
    needed_fiat = sum(o["price"] * o["remaining"] for o in open_orders if o["side"] == "buy")
    needed_crypto = sum(o["remaining"] for o in open_orders if o["side"] == "sell")
    entries = [e for e in snapshot.pending_placements if e["order_type"] == "limit"]
    needed_fiat += sum(e["price"] * e["quantity"] for e in entries if e["side"] == "buy")
    needed_crypto += sum(e["quantity"] for e in entries if e["side"] == "sell")
    balance = snapshot.balance_state
    reserved_fiat = float(balance["reserved_fiat"])
    reserved_crypto = float(balance["reserved_crypto"])
    problems = []
    if abs(reserved_fiat - needed_fiat) > 1e-6:
        problems.append(f"reserved fiat {reserved_fiat} for open buys needing {needed_fiat}")
    if abs(reserved_crypto - needed_crypto) > 1e-6:
        problems.append(f"reserved crypto {reserved_crypto} for open sells needing {needed_crypto}")
    waiting = {(lvl["price"], lvl["state"]) for lvl in snapshot.grid_levels if lvl["state"].startswith("waiting_for")}
    holding = {
        (o["grid_level_price"], f"waiting_for_{o['side']}_fill")
        for o in open_orders
        if o["grid_level_price"] is not None
    }
    holding |= {(e["level_price"], f"waiting_for_{e['side']}_fill") for e in entries}
    if waiting != holding:
        problems.append(f"waiting levels {sorted(waiting)} but open orders on {sorted(holding)}")
    return problems


def _config(tmp_path, trading_mode="live", num_grids=5):
    config = {
        "exchange": {"name": "binance", "trading_fee": 0.001, "trading_mode": trading_mode},
        "pair": {"base_currency": "BTC", "quote_currency": "USDT"},
        "trading_settings": {
            "initial_balance": 1000,
            "timeframe": "1m",
            "period": {"start_date": "2024-07-04T00:00:00Z", "end_date": "2024-07-11T00:00:00Z"},
        },
        "grid_strategy": {
            "type": "simple_grid",
            "spacing": "arithmetic",
            "num_grids": num_grids,
            "range": {"top": 120, "bottom": 80},
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
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


def _bot(config_path) -> GridTradingBot:
    config_manager = ConfigManager(str(config_path))
    event_bus = EventBus()
    notification_handler = NotificationHandler(None, enabled=False)
    bot = GridTradingBot(
        str(config_path),
        config_manager,
        notification_handler,
        event_bus,
        no_plot=True,
        run_files=RunFiles.for_config(config_manager),
    )
    bot.order_status_tracker.polling_interval = 0.05
    return bot


class Running:
    """A bot running in the background of the test's event loop."""

    def __init__(self, bot: GridTradingBot):
        self.bot = bot
        self.task = asyncio.create_task(bot.run())

    async def until(self, condition, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if self.task.done():
                await self.task
                raise AssertionError(f"the bot stopped before the condition held: {self.bot.stop_request}")
            assert time.monotonic() < deadline, "the condition never held"
            await asyncio.sleep(0.02)

    async def stop(self) -> None:
        await self.bot.request_stop(StopRequest(StopKind.SIGNAL, "test stop", signal_number=signal.SIGTERM))
        await asyncio.wait_for(self.task, timeout=20.0)


async def _armed_then_stopped(exchange, config_path) -> None:
    first = Running(_bot(config_path))
    await first.until(lambda: len(exchange.open_orders()) == ARMED_ORDERS)
    await first.stop()


def _database(tmp_path):
    return tmp_path / "data" / "state.db"


def _dump(tmp_path) -> str:
    with closing(sqlite3.connect(_database(tmp_path))) as database:
        return "\n".join(database.iterdump())


def _saved(tmp_path):
    with closing(sqlite3.connect(_database(tmp_path))) as database:
        database.row_factory = sqlite3.Row
        balance = database.execute("SELECT * FROM balance_state").fetchone()
        orders = database.execute("SELECT identifier, status, filled FROM orders").fetchall()
    return SimpleNamespace(
        open_ids={row["identifier"] for row in orders if row["status"] == "open"},
        filled={row["identifier"]: row["filled"] for row in orders},
        fiat_total=float(balance["fiat_balance"]) + float(balance["reserved_fiat"]),
        crypto_total=float(balance["crypto_balance"]) + float(balance["reserved_crypto"]),
        total_fees=float(balance["total_fees"]),
        realized_pnl=float(balance["realized_pnl"]),
    )


def _saved_entries(tmp_path) -> list[str]:
    with closing(sqlite3.connect(_database(tmp_path))) as database:
        return [row[0] for row in database.execute("SELECT client_order_id FROM pending_placements")]


def _expected_realized_pnl(executions) -> float:
    ledger = PnlLedger()
    position = Decimal("0")
    for execution in executions:
        quantity, price, fee = (Decimal(str(execution[key])) for key in ("quantity", "price", "fee"))
        if execution["side"] == "buy":
            received = quantity - fee
            ledger.record_buy(position, received, price, fee * price)
            position += received
        else:
            ledger.record_sell(position, quantity, price, fee)
            position -= quantity
    return float(ledger.realized_pnl)


@pytest.mark.parametrize("trading_mode", ["live", "paper_trading"])
async def test_a_restart_resumes_the_saved_state_and_books_every_fill_once(tmp_path, exchange, checks, trading_mode):
    config_path = _config(tmp_path, trading_mode)

    first = Running(_bot(config_path))
    await first.until(lambda: len(exchange.open_orders()) == ARMED_ORDERS)
    exchange.fill(exchange.order_at(100.0, "buy")["id"])
    await first.until(lambda: exchange.has_open(110.0, "sell"))
    await first.stop()

    buy_90 = exchange.order_at(90.0, "buy")
    buy_80 = exchange.order_at(80.0, "buy")
    cancelled_sell = exchange.order_at(120.0, "sell")
    exchange.fill(buy_90["id"])
    exchange.fill(buy_80["id"], buy_80["amount"] * 0.4)
    exchange.cancel(cancelled_sell["id"])
    exchange.remove(exchange.order_at(110.0, "sell")["id"])

    second = Running(_bot(config_path))
    # The sell of buy 90 takes the level the vanished sell freed; the cancelled 120 sell is re-placed.
    await second.until(lambda: exchange.has_open(110.0, "sell") and exchange.has_open(120.0, "sell"))
    assert exchange.order_at(120.0, "sell")["id"] != cancelled_sell["id"]
    assert _saved(tmp_path).filled[buy_80["id"]] == pytest.approx(buy_80["amount"] * 0.4)
    exchange.fill(buy_80["id"])
    await second.until(lambda: len([o for o in exchange.open_orders("sell") if o["price"] == 110.0]) == 2)
    exchange.fill(exchange.order_at(120.0, "sell")["id"])
    await second.until(lambda: exchange.has_open(100.0, "buy"))
    await second.stop()

    saved = _saved(tmp_path)
    assert saved.open_ids == {order["id"] for order in exchange.open_orders()}
    totals = exchange.totals()
    assert saved.fiat_total == pytest.approx(totals["USDT"], abs=1e-6)
    assert saved.crypto_total == pytest.approx(totals["BTC"], abs=1e-6)
    assert saved.total_fees == pytest.approx(exchange.fees_paid(), abs=1e-6)
    assert saved.realized_pnl == pytest.approx(_expected_realized_pnl(exchange.executions()), abs=1e-4)
    assert checks.saves > 0
    assert checks.problems == []


async def test_the_fee_the_exchange_reports_is_the_one_booked(tmp_path, exchange):
    ExchangeState.create(exchange.path, price=ARMING_PRICE, quote=1000.0, fee=0.002, report_fees=True)
    bot = Running(_bot(_config(tmp_path)))
    await bot.until(lambda: len(exchange.open_orders()) == ARMED_ORDERS)
    buy = exchange.order_at(100.0, "buy")
    exchange.fill(buy["id"])
    await bot.until(lambda: exchange.has_open(110.0, "sell"))
    await bot.stop()

    assert exchange.order_at(110.0, "sell")["amount"] == pytest.approx(buy["amount"] * (1 - 0.002))
    saved = _saved(tmp_path)
    totals = exchange.totals()
    assert saved.crypto_total == pytest.approx(totals["BTC"], abs=1e-6)
    assert saved.fiat_total == pytest.approx(totals["USDT"], abs=1e-6)
    assert saved.total_fees == pytest.approx(exchange.fees_paid(), abs=1e-6)


async def test_a_fresh_start_over_open_orders_is_refused(tmp_path, exchange):
    exchange.add_open_order("buy", 95.0, 0.5)
    bot = _bot(_config(tmp_path))

    await asyncio.wait_for(bot.run(), timeout=20)

    assert bot.stop_request.kind is StopKind.START_REFUSED
    assert bot.stop_request.exit_code == 0
    assert len(exchange.read()["orders"]) == 1
    with closing(sqlite3.connect(_database(tmp_path))) as database:
        assert database.execute("SELECT COUNT(*) FROM bot_state").fetchone()[0] == 0


# What the pre-schema hash function gave for this test's config written without db_path.
_PRE_SCHEMA_HASH = "f399cc1653773ef9e2a614bff0cb4b6a106f4e847ef531d977d8a19730496f4e"


async def test_a_state_saved_under_the_pre_schema_hash_is_not_found_and_the_fresh_start_is_refused(
    tmp_path, exchange, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    config_path = _config(tmp_path)
    raw = json.loads(config_path.read_text())
    del raw["persistence"]["db_path"]
    config_path.write_text(json.dumps(raw))
    await _armed_then_stopped(exchange, config_path)
    current_db = tmp_path / ConfigManager(str(config_path)).get_state_db_path()
    old_db = current_db.with_name(f"state_{_PRE_SCHEMA_HASH[:8]}.db")
    current_db.rename(old_db)
    with closing(sqlite3.connect(old_db)) as database:
        database.execute("UPDATE bot_state SET config_hash = ?", (_PRE_SCHEMA_HASH,))
        database.commit()
        saved_before = "\n".join(database.iterdump())
    orders_before = exchange.read()["orders"]

    bot = _bot(config_path)
    await asyncio.wait_for(bot.run(), timeout=20)

    assert bot.stop_request.kind is StopKind.START_REFUSED
    assert bot.stop_request.exit_code == 0
    assert exchange.read()["orders"] == orders_before
    with closing(sqlite3.connect(old_db)) as database:
        assert "\n".join(database.iterdump()) == saved_before
    with closing(sqlite3.connect(current_db)) as database:
        assert database.execute("SELECT COUNT(*) FROM bot_state").fetchone()[0] == 0


async def test_a_corrupt_saved_state_is_refused_and_left_untouched(tmp_path, exchange):
    config_path = _config(tmp_path)
    await _armed_then_stopped(exchange, config_path)
    with closing(sqlite3.connect(_database(tmp_path))) as database:
        database.execute(
            "UPDATE orders SET status = 'bogus' "
            "WHERE identifier = (SELECT identifier FROM orders WHERE status = 'open' LIMIT 1)"
        )
        database.commit()
    saved_before = _dump(tmp_path)
    orders_before = exchange.read()["orders"]

    bot = _bot(config_path)
    await asyncio.wait_for(bot.run(), timeout=20)

    assert bot.stop_request.kind is StopKind.START_REFUSED
    assert "bogus" in bot.stop_request.reason
    assert _dump(tmp_path) == saved_before
    assert exchange.read()["orders"] == orders_before


async def test_a_changed_grid_on_the_same_database_is_refused(tmp_path, exchange):
    config_path = _config(tmp_path)
    await _armed_then_stopped(exchange, config_path)
    saved_before = _dump(tmp_path)
    _config(tmp_path, num_grids=6)

    bot = _bot(config_path)
    await asyncio.wait_for(bot.run(), timeout=20)

    assert bot.stop_request.kind is StopKind.START_REFUSED
    assert "another grid configuration" in bot.stop_request.reason
    assert _dump(tmp_path) == saved_before


async def test_an_unreachable_exchange_defers_the_start_and_keeps_the_state(tmp_path, exchange):
    config_path = _config(tmp_path)
    await _armed_then_stopped(exchange, config_path)
    saved_before = _dump(tmp_path)
    exchange.fail("fetch_order")

    bot = _bot(config_path)
    await asyncio.wait_for(bot.run(), timeout=20)

    assert bot.stop_request.kind is StopKind.EXCHANGE_UNAVAILABLE
    assert bot.stop_request.exit_code == 75
    assert _dump(tmp_path) == saved_before


async def test_a_sent_placement_found_on_the_exchange_is_adopted_after_a_restart(tmp_path, exchange, checks):
    config_path = _config(tmp_path)
    await _armed_then_stopped(exchange, config_path)
    # A buy for the free 110 level reached the exchange, but the bot stopped before its next full save.
    config_hash = ConfigManager(str(config_path)).get_config_hash()
    entry = PendingPlacement(
        client_order_id=new_client_order_id(config_hash),
        order_type=OrderType.LIMIT,
        side=OrderSide.BUY,
        quantity=0.2,
        price=110.0,
        purpose=PlacementPurpose.GRID,
        level_price=110.0,
        sent=True,
        created_at=1,
    )
    order_id = exchange.add_open_order("buy", 110.0, 0.2)
    exchange.update(lambda data: data["orders"][order_id].update(clientOrderId=entry.client_order_id))
    repository = SQLiteStateRepository(str(_database(tmp_path)))
    repository.initialize()
    repository.save_pending(pending_placement_to_dict(entry))
    repository.close()

    second = Running(_bot(config_path))
    await second.until(lambda: second.bot.order_book.get_order(order_id) is not None)
    await second.stop()

    saved = _saved(tmp_path)
    assert order_id in saved.open_ids
    assert saved.open_ids == {order["id"] for order in exchange.open_orders()}
    assert _saved_entries(tmp_path) == []
    totals = exchange.totals()
    assert saved.fiat_total == pytest.approx(totals["USDT"], abs=1e-6)
    assert saved.crypto_total == pytest.approx(totals["BTC"], abs=1e-6)
    assert checks.problems == []
