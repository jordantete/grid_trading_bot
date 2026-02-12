"""Smoke tests for exchange dependencies (ccxt, aiohttp, WebSocket).

These tests make real network calls to public exchange endpoints.
They are excluded from the default test run and can be invoked with:

    uv run pytest -m sandbox tests/integration/test_sandbox_smoke.py -v

All tests skip gracefully if the network is unavailable.
"""

import asyncio

import pytest


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
