"""The fake exchange answers the order calls LiveExchangeService makes the way ccxt does."""

import asyncio
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import ccxt
import pytest

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.services import live_exchange_service
from grid_trading_bot.core.services.exceptions import OrderNotFoundError
from grid_trading_bot.core.services.live_exchange_service import LiveExchangeService
from tests.fakes.fake_exchange import PAIR, ExchangeState, FakeExchange

CLIENT_ORDER_ID = "g3f2a9c1d00112233aabbccdd"


@pytest.fixture
def service(tmp_path, monkeypatch):
    state = ExchangeState.create(tmp_path / "exchange.json")
    monkeypatch.setattr(FakeExchange, "state_path", state.path)
    monkeypatch.setattr(live_exchange_service, "ccxtpro", SimpleNamespace(binance=FakeExchange))
    monkeypatch.setenv("EXCHANGE_API_KEY", "test")
    monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test")
    config_manager = Mock(spec=ConfigManager)
    config_manager.get_exchange_name.return_value = "binance"
    config_manager.get_websocket_max_retries.return_value = 5
    config_manager.get_websocket_retry_base_delay.return_value = 5
    config_manager.get_circuit_breaker_failure_threshold.return_value = 5
    config_manager.get_circuit_breaker_recovery_timeout.return_value = 60.0
    config_manager.get_circuit_breaker_half_open_max_calls.return_value = 1
    config_manager.get_post_only.return_value = False
    return LiveExchangeService(config_manager, is_paper_trading_activated=False)


async def test_an_order_is_found_by_the_client_order_id_it_was_placed_with(service):
    placed = await service.place_order(PAIR, "limit", "buy", 1.0, 90.0, client_order_id=CLIENT_ORDER_ID)

    found = await service.fetch_order_by_client_id(CLIENT_ORDER_ID, PAIR)

    assert found["id"] == placed["id"]
    assert found["clientOrderId"] == CLIENT_ORDER_ID


async def test_a_client_order_id_the_exchange_never_received_is_not_found(service):
    await service.place_order(PAIR, "limit", "buy", 1.0, 90.0, client_order_id=CLIENT_ORDER_ID)

    with pytest.raises(OrderNotFoundError):
        await service.fetch_order_by_client_id("g3f2a9c1d99999999aabbccdd", PAIR)


async def test_an_order_is_still_fetched_by_its_exchange_id(service):
    placed = await service.place_order(PAIR, "limit", "buy", 1.0, 90.0, client_order_id=CLIENT_ORDER_ID)

    fetched = await service.fetch_order(placed["id"], PAIR)

    assert fetched["clientOrderId"] == CLIENT_ORDER_ID


@pytest.fixture
def state(tmp_path, monkeypatch):
    exchange_state = ExchangeState.create(tmp_path / "exchange.json", price=100.0, quote=1000.0, base=10.0)
    monkeypatch.setattr(FakeExchange, "state_path", exchange_state.path)
    return exchange_state


@pytest.fixture
def fake(state):
    return FakeExchange({})


async def test_a_scripted_failure_stops_after_its_count(fake, state):
    state.fail("fetch_balance", times=2)

    for _ in range(2):
        with pytest.raises(ccxt.NetworkError):
            await fake.fetch_balance()

    assert (await fake.fetch_balance())["free"]["USDT"] == 1000.0


async def test_a_failure_without_a_count_keeps_failing(fake, state):
    state.fail("fetch_open_orders")

    for _ in range(3):
        with pytest.raises(ccxt.NetworkError):
            await fake.fetch_open_orders(PAIR)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("network", ccxt.NetworkError),
        ("timeout", ccxt.RequestTimeout),
        ("insufficient_funds", ccxt.InsufficientFunds),
        ("duplicate", ccxt.DuplicateOrderId),
    ],
)
async def test_each_error_kind_raises_its_ccxt_error(fake, state, error, expected):
    order_id = state.add_open_order("buy", 90.0, 1.0)
    state.fail("cancel_order", times=1, error=error)

    with pytest.raises(ccxt.BaseError) as raised:
        await fake.cancel_order(order_id, PAIR)

    assert type(raised.value) is expected
    assert [order["id"] for order in state.open_orders()] == [order_id]


def test_an_unknown_error_kind_is_refused(state):
    with pytest.raises(ValueError, match="unknown error"):
        state.fail("create_order", error="meteor")


async def test_a_failure_after_accept_creates_the_order_then_loses_the_answer(fake, state):
    state.fail("create_order", times=1, error="timeout", after_accept=True)

    with pytest.raises(ccxt.RequestTimeout):
        await fake.create_order(PAIR, "limit", "buy", 1.0, 90.0, params={"clientOrderId": "gabc"})

    [order] = state.open_orders()
    assert order["clientOrderId"] == "gabc"
    found = await fake.fetch_order(None, PAIR, params={"clientOrderId": "gabc"})
    assert found["id"] == order["id"]


async def test_a_second_open_order_with_the_same_client_id_is_refused(fake, state):
    await fake.create_order(PAIR, "limit", "buy", 1.0, 90.0, params={"clientOrderId": "gabc"})

    with pytest.raises(ccxt.DuplicateOrderId):
        await fake.create_order(PAIR, "limit", "buy", 1.0, 90.0, params={"clientOrderId": "gabc"})

    assert len(state.open_orders()) == 1


async def test_a_delayed_answer_creates_the_order_before_it_returns(fake, state):
    state.set_scenario(response_delay=0.3)

    placing = asyncio.create_task(fake.create_order(PAIR, "limit", "buy", 1.0, 90.0, params={"clientOrderId": "gdef"}))
    await asyncio.sleep(0.1)

    assert len(state.open_orders()) == 1
    assert not placing.done()
    assert (await placing)["clientOrderId"] == "gdef"


async def test_a_delayed_cancellation_cancels_after_its_delay(fake, state):
    order_id = state.add_open_order("buy", 90.0, 1.0)
    state.set_scenario(cancel_delay=0.3)

    cancelling = asyncio.create_task(fake.cancel_order(order_id, PAIR))
    await asyncio.sleep(0.1)

    assert len(state.open_orders()) == 1
    await cancelling
    assert state.open_orders() == []


def test_fill_all_fills_every_order_in_one_write(state):
    first = state.add_open_order("buy", 90.0, 1.0)
    second = state.add_open_order("buy", 94.0, 1.0)

    state.fill_all([first, second])

    orders = state.read()["orders"]
    assert [orders[first]["status"], orders[second]["status"]] == ["closed", "closed"]


async def test_set_scenario_changes_the_ticker_price_and_refuses_other_fields(fake, state):
    state.set_scenario(price=94.0)

    assert (await fake.fetch_ticker(PAIR))["last"] == 94.0
    with pytest.raises(ValueError, match="not settable"):
        state.set_scenario(fee=0.0)


@pytest.mark.parametrize("params", [None, {}])
async def test_a_lookup_without_an_order_id_or_a_client_id_finds_nothing(fake, state, params):
    state.add_open_order("buy", 90.0, 1.0)

    with pytest.raises(ccxt.OrderNotFound):
        await fake.fetch_order(None, PAIR, params=params)


def _add_orders(path: str, count: int) -> None:
    state = ExchangeState(Path(path))
    for _ in range(count):
        state.add_open_order("buy", 1.0, 0.001)


def test_updates_from_several_processes_never_lose_one_another(state):
    workers = [
        multiprocessing.get_context("spawn").Process(target=_add_orders, args=(str(state.path), 50)) for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)

    assert [worker.exitcode for worker in workers] == [0] * 4
    assert len(state.read()["orders"]) == 200
