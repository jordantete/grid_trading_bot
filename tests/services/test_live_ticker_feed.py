import asyncio
from unittest.mock import AsyncMock, patch

import ccxt
import pytest

from grid_trading_bot.core.services.exceptions import DataFetchError
from grid_trading_bot.core.services.live_ticker_feed import LiveTickerFeed


def _feed(exchange=None, **overrides) -> LiveTickerFeed:
    settings = {
        "max_price_deviation": 0.50,
        "max_consecutive_price_rejections": 5,
        "websocket_max_retries": 5,
        "websocket_retry_base_delay": 5,
    }
    return LiveTickerFeed(exchange or AsyncMock(), **{**settings, **overrides})


class TestValidatePrice:
    def test_validate_price_valid(self):
        feed = _feed()

        result = feed.validate_price(50000.0, "BTC/USD")

        assert result == 50000.0
        assert feed.last_price == 50000.0

    def test_validate_price_none(self):
        assert _feed().validate_price(None, "BTC/USD") is None

    def test_validate_price_nan(self):
        assert _feed().validate_price(float("nan"), "BTC/USD") is None

    def test_validate_price_negative(self):
        assert _feed().validate_price(-100.0, "BTC/USD") is None

    def test_validate_price_zero(self):
        assert _feed().validate_price(0.0, "BTC/USD") is None

    def test_validate_price_infinity(self):
        assert _feed().validate_price(float("inf"), "BTC/USD") is None

    def test_validate_price_excessive_deviation(self):
        feed = _feed()
        feed.validate_price(50000.0, "BTC/USD")

        # 100 % deviation is rejected
        assert feed.validate_price(100000.0, "BTC/USD") is None

        # 40 % deviation is accepted
        assert feed.validate_price(70000.0, "BTC/USD") == 70000.0

    def test_last_price_follows_accepted_prices_only(self):
        feed = _feed()

        assert feed.last_price is None
        feed.validate_price(50000.0, "BTC/USD")
        feed.validate_price(100000.0, "BTC/USD")  # rejected: 100 % deviation

        assert feed.last_price == 50000.0


class TestDeviationAnchorRelease:
    @pytest.fixture
    def feed(self):
        feed = _feed()
        feed.validate_price(100.0, "SOL/USDT")
        return feed

    def test_reanchors_after_five_consecutive_rejections(self, feed):
        for _ in range(4):
            assert feed.validate_price(200.0, "SOL/USDT") is None
        assert feed.validate_price(200.0, "SOL/USDT") == 200.0
        assert feed.last_price == 200.0

    def test_accepted_price_resets_rejection_counter(self, feed):
        for _ in range(3):
            feed.validate_price(200.0, "SOL/USDT")
        feed.validate_price(101.0, "SOL/USDT")  # accepted
        for _ in range(4):
            assert feed.validate_price(200.0, "SOL/USDT") is None  # counter restarted

    def test_invalid_rejections_do_not_touch_counter(self, feed):
        for _ in range(3):
            feed.validate_price(200.0, "SOL/USDT")
        # None/non-numeric/non-finite/non-positive rejections must not advance the counter
        for bad_value in (None, "not-a-number", float("nan"), float("inf"), -5.0, 0.0):
            assert feed.validate_price(bad_value, "SOL/USDT") is None
        # counter should still be at 3, so one more deviation rejection stays rejected...
        assert feed.validate_price(200.0, "SOL/USDT") is None
        # ...and the 5th deviation rejection re-anchors
        assert feed.validate_price(200.0, "SOL/USDT") == 200.0

    def test_accepted_price_records_timestamp(self):
        feed = _feed()

        assert feed.last_price_update_ts is None
        feed.validate_price(100.0, "SOL/USDT")
        assert feed.last_price_update_ts is not None


class TestSubscribe:
    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    async def test_subscribe_to_ticker_updates_success(self):
        exchange = AsyncMock()
        exchange.watch_ticker = AsyncMock(side_effect=[{"last": 50000.0}, asyncio.CancelledError()])
        on_ticker_update = AsyncMock()
        feed = _feed(exchange)

        await feed.subscribe("BTC/USD", on_ticker_update, 0.1)

        on_ticker_update.assert_awaited_once_with(50000.0)
        assert not feed.connection_active

    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    async def test_subscribe_to_ticker_updates_skips_invalid_price(self):
        exchange = AsyncMock()
        exchange.watch_ticker = AsyncMock(side_effect=[{"last": None}, {"last": 50000.0}, asyncio.CancelledError()])
        on_ticker_update = AsyncMock()
        feed = _feed(exchange)

        await feed.subscribe("BTC/USD", on_ticker_update, 0.1)

        on_ticker_update.assert_awaited_once_with(50000.0)
        assert not feed.connection_active

    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    @patch("grid_trading_bot.core.services.live_ticker_feed.asyncio.sleep", new_callable=AsyncMock)
    async def test_subscribe_to_ticker_updates_network_error(self, mock_sleep):
        exchange = AsyncMock()
        exchange.watch_ticker = AsyncMock(side_effect=[ccxt.NetworkError("Network issue"), asyncio.CancelledError()])
        on_ticker_update = AsyncMock()
        feed = _feed(exchange, websocket_max_retries=1)

        with patch.object(feed.logger, "error") as mock_logger_error:
            with pytest.raises(DataFetchError, match="failed after 1 retries"):
                await feed.subscribe("BTC/USD", on_ticker_update, 0.1)

            mock_logger_error.assert_any_call(
                "Error connecting to WebSocket for BTC/USD: Network issue. Retrying in 5 seconds (1/1).",
            )

            on_ticker_update.assert_not_awaited()

        assert not feed.connection_active
        assert exchange.watch_ticker.await_count == 1

    @pytest.mark.asyncio
    async def test_subscribe_to_ticker_updates_max_retries_exceeded(self):
        exchange = AsyncMock()
        exchange.watch_ticker = AsyncMock(side_effect=ccxt.NetworkError("Network issue"))
        feed = _feed(exchange, websocket_max_retries=2, websocket_retry_base_delay=0)

        with pytest.raises(DataFetchError, match="failed after 2 retries"):
            await feed.subscribe("BTC/USD", AsyncMock(), 0.1)

        assert not feed.connection_active
        assert exchange.watch_ticker.await_count == 2

    @pytest.mark.asyncio
    async def test_the_ticker_loop_never_closes_the_exchange(self):
        exchange = AsyncMock()
        exchange.watch_ticker = AsyncMock(side_effect=asyncio.CancelledError())
        exchange.close = AsyncMock()
        feed = _feed(exchange)

        await feed.subscribe("BTC/USD", AsyncMock(), 0.1)

        exchange.close.assert_not_awaited()
