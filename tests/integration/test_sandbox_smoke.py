"""Smoke tests for ccxt against live exchange endpoints (REST, WebSocket, and the aiohttp transport ccxt brings).

These tests make real network calls to public exchange endpoints.
They are excluded from the default test run and can be invoked with:

    uv run pytest -m sandbox tests/integration/test_sandbox_smoke.py -v

All tests skip gracefully if the network is unavailable.
"""

import asyncio
import os

import pytest

from tests.fakes.config import config_manager_from


def _network_unavailable() -> bool:
    """Quick check: can we resolve a DNS name?"""
    import socket

    try:
        socket.create_connection(("api.binance.com", 443), timeout=5)
    except OSError:
        return True
    return False


skip_if_no_network = pytest.mark.skipif(
    _network_unavailable(),
    reason="Network unavailable — skipping sandbox test",
)


# ---------------------------------------------------------------------------
# F5: Import and instantiation
# ---------------------------------------------------------------------------


@pytest.mark.sandbox
def test_ccxt_import_and_instantiation():
    """ccxt and ccxt.pro can be imported and a Binance exchange instantiated."""
    import ccxt
    import ccxt.pro

    exchange = ccxt.binance()
    assert hasattr(exchange, "fetch_ticker")
    assert hasattr(exchange, "load_markets")
    assert hasattr(exchange, "fetch_ohlcv")

    pro_exchange = ccxt.pro.binance()
    assert hasattr(pro_exchange, "watch_ticker")


# ---------------------------------------------------------------------------
# F6: Public API calls
# ---------------------------------------------------------------------------


@pytest.mark.sandbox
@skip_if_no_network
@pytest.mark.timeout(30)
async def test_ccxt_public_api():
    """Public REST calls to Binance succeed and return expected structure."""
    import ccxt.async_support as ccxt_async

    exchange = ccxt_async.binance()
    try:
        markets = await exchange.load_markets()
        assert isinstance(markets, dict)
        assert len(markets) > 0
        assert "SOL/USDT" in markets, "SOL/USDT pair not found in Binance markets"

        ticker = await exchange.fetch_ticker("SOL/USDT")
        assert isinstance(ticker, dict)
        for key in ("last", "bid", "ask"):
            assert key in ticker, f"Ticker missing key: {key}"
            assert isinstance(ticker[key], int | float), f"Ticker[{key}] is not numeric: {ticker[key]}"
            assert ticker[key] > 0, f"Ticker[{key}] is not positive: {ticker[key]}"
    finally:
        await exchange.close()


# ---------------------------------------------------------------------------
# F7: WebSocket connectivity
# ---------------------------------------------------------------------------


@pytest.mark.sandbox
@skip_if_no_network
@pytest.mark.timeout(30)
async def test_ccxt_websocket_connection():
    """Open a WebSocket, receive at least one ticker, and close cleanly."""
    import ccxt.pro as ccxt_pro

    exchange = ccxt_pro.binance()
    try:
        ticker = await asyncio.wait_for(
            exchange.watch_ticker("SOL/USDT"),
            timeout=15,
        )
        assert isinstance(ticker, dict)
        assert "last" in ticker
        assert "timestamp" in ticker
        assert isinstance(ticker["last"], int | float)
        assert ticker["last"] > 0
    finally:
        await exchange.close()


# ---------------------------------------------------------------------------
# F8: Client order id round trip on the Binance testnet and the OKX demo
# ---------------------------------------------------------------------------

TEST_VENUES = {
    "binance-testnet": {
        "keys": {"EXCHANGE_API_KEY": "BINANCE_TESTNET_API_KEY", "EXCHANGE_SECRET_KEY": "BINANCE_TESTNET_SECRET_KEY"},
        "exchange": lambda: "binance",
        "pair": "BTC/USDT",
        "is_test_environment": lambda exchange: "testnet" in str(exchange.urls["api"]),
    },
    "okx-demo": {
        "keys": {
            "EXCHANGE_API_KEY": "OKX_DEMO_API_KEY",
            "EXCHANGE_SECRET_KEY": "OKX_DEMO_SECRET_KEY",
            "EXCHANGE_PASSWORD": "OKX_DEMO_PASSWORD",
        },
        # myokx for an OKX Europe account (my.okx.com), okx for a global one (www.okx.com).
        "exchange": lambda: os.getenv("OKX_DEMO_EXCHANGE", "myokx"),
        "pair": "BTC/USDC",
        "is_test_environment": lambda exchange: (exchange.headers or {}).get("x-simulated-trading") == "1",
    },
}


def _venue_param(name: str):
    missing = [key for key in TEST_VENUES[name]["keys"].values() if not os.getenv(key)]
    return pytest.param(
        name,
        id=name,
        marks=pytest.mark.skipif(bool(missing), reason=f"{name} keys not set ({', '.join(missing)})"),
    )


@pytest.mark.sandbox
@skip_if_no_network
@pytest.mark.timeout(60)
@pytest.mark.parametrize("venue", [_venue_param(name) for name in TEST_VENUES])
async def test_a_test_order_is_found_by_its_client_id_before_and_after_its_cancel(monkeypatch, valid_config, venue):
    """A tiny post-only buy far under the price, placed with a client id, is found by it while open and cancelled."""
    from grid_trading_bot.core.order_handling.placement_journal import new_client_order_id
    from grid_trading_bot.core.services.live_exchange_service import LiveExchangeService

    spec = TEST_VENUES[venue]
    for bot_key, test_key in spec["keys"].items():
        monkeypatch.setenv(bot_key, os.environ[test_key])
    config_manager = config_manager_from(
        valid_config,
        {
            "exchange": {"name": spec["exchange"](), "post_only": True},
            "execution": {"websocket_max_retries": 1, "websocket_retry_base_delay": 1},
        },
    )
    service = LiveExchangeService(config_manager, is_paper_trading_activated=True)
    assert spec["is_test_environment"](service.exchange), "refusing to place an order outside the test environment"

    pair = spec["pair"]
    client_order_id = new_client_order_id("5a4d0b0e")
    open_order_id = None
    try:
        await service.load_exchange_markets(pair)
        price = service.round_price(pair, await service.get_current_price(pair) * 0.8)
        constraints = service.get_market_constraints(pair)
        amount = service.round_amount(
            pair, max(constraints.min_amount or 0.0, 2 * (constraints.min_cost or 10.0) / price)
        )

        placed = await service.place_order(pair, "limit", "buy", amount, price, client_order_id=client_order_id)
        open_order_id = placed["id"]
        found = await service.fetch_order_by_client_id(client_order_id, pair)
        assert (found["id"], found["clientOrderId"], found["status"]) == (placed["id"], client_order_id, "open")

        await service.cancel_order(placed["id"], pair)
        open_order_id = None
        cancelled = await service.fetch_order_by_client_id(client_order_id, pair)
        assert (cancelled["id"], cancelled["status"]) == (placed["id"], "canceled")
    finally:
        if open_order_id is not None:
            await service.cancel_order(open_order_id, pair)
        await service.close()
