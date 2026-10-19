"""The fake exchange answers the order calls LiveExchangeService makes the way ccxt does."""

from types import SimpleNamespace
from unittest.mock import Mock

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
