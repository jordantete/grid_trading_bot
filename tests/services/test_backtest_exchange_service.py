from unittest.mock import AsyncMock, Mock, patch

import ccxt
import pandas as pd
import pytest
from tests.fakes.config import config_manager_from

from grid_trading_bot.core.services.backtest_exchange_service import BacktestExchangeService
from grid_trading_bot.core.services.exceptions import (
    DataFetchError,
    HistoricalMarketDataFileNotFoundError,
    UnsupportedExchangeError,
    UnsupportedPairError,
    UnsupportedTimeframeError,
)
from grid_trading_bot.core.services.market_constraints import MarketConstraints

HOUR_MS = 3_600_000
START_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z


class CappingExchange:
    """A ccxt exchange stand-in that, like the real ones, returns at most `max_per_call` candles."""

    def __init__(self, max_per_call: int, candles: list[list[float]]):
        self.timeframes = {"1h": "1h"}
        self.max_per_call = max_per_call
        self.candles = candles

    @staticmethod
    def parse8601(date: str) -> int:
        return int(pd.Timestamp(date).timestamp() * 1000)

    def load_markets(self):
        return {"BTC/USDT": {}}

    def fetch_ohlcv(self, pair, timeframe, since=None, limit=None):
        available = [candle for candle in self.candles if candle[0] >= since]
        return available[: min(limit or 500, self.max_per_call)]


def _hourly_candles(count: int) -> list[list[float]]:
    return [[START_MS + i * HOUR_MS, 1, 2, 0.5, 1.5, 10] for i in range(count)]


def _capping_exchange(monkeypatch, name: str, max_per_call: int, candle_count: int) -> None:
    candles = _hourly_candles(candle_count)
    monkeypatch.setattr(ccxt, name, lambda: CappingExchange(max_per_call, candles), raising=False)


class TestBacktestExchangeService:
    @pytest.fixture
    def config_manager(self, valid_config):
        return config_manager_from(
            valid_config,
            {"exchange": {"name": "binance"}, "trading_settings": {"historical_data_file": "data/test_data.csv"}},
        )

    @pytest.fixture
    def backtest_service(self, config_manager):
        return BacktestExchangeService(config_manager)

    def test_backtest_market_constraints_come_from_the_config(self, valid_config):
        service = BacktestExchangeService(
            config_manager_from(
                valid_config,
                {"exchange": {"name": "binance"}, "execution": {"backtest_min_amount": 0.001, "backtest_min_cost": 5}},
            )
        )

        assert service.get_market_constraints("BTC/USDT") == MarketConstraints(0.001, 5)

    def test_backtest_market_constraints_are_none_without_minimums(self, backtest_service):
        assert backtest_service.get_market_constraints("BTC/USDT") is None

    async def test_backtest_loads_no_markets_and_has_no_feed_time(self, backtest_service):
        assert await backtest_service.load_exchange_markets("BTC/USDT") is None
        assert backtest_service.last_price_update_ts is None

    @patch("grid_trading_bot.core.services.backtest_exchange_service.ccxt.binance")
    def test_initialization_supported_exchange(self, mock_ccxt, config_manager):
        service = BacktestExchangeService(config_manager)
        mock_ccxt.assert_called_once()
        assert service.exchange_name == "binance", "Expected exchange name to be 'binance'"

    def test_initialization_unsupported_exchange(self, valid_config):
        config_manager = config_manager_from(valid_config, {"exchange": {"name": "unsupported_exchange"}})
        with pytest.raises(UnsupportedExchangeError):
            BacktestExchangeService(config_manager)

    @patch("pandas.read_csv")
    @patch("os.path.exists", return_value=True)
    def test_load_ohlcv_from_file_success(self, mock_exists, mock_read_csv, config_manager):
        mock_data = pd.DataFrame(
            {
                "timestamp": pd.date_range(start="2023-01-01", periods=5, freq="D"),
                "open": [100, 101, 102, 103, 104],
                "high": [105, 106, 107, 108, 109],
                "low": [95, 96, 97, 98, 99],
                "close": [100, 101, 102, 103, 104],
                "volume": [1000, 1100, 1200, 1300, 1400],
            },
        )
        mock_read_csv.return_value = mock_data

        service = BacktestExchangeService(config_manager)
        result = service.fetch_ohlcv("BTC/USD", "1d", "2023-01-01", "2023-01-05")

        assert isinstance(result, pd.DataFrame), "Expected result to be a DataFrame"
        assert len(result) == 5, "Expected 5 rows of data"
        pd.testing.assert_frame_equal(result, mock_data.set_index("timestamp"))

    def test_a_date_range_without_candles_names_the_pair_the_dates_and_the_file(self, tmp_path, valid_config):
        csv = tmp_path / "candles.csv"
        csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            "2024-08-01 00:00:00,171.7,172.15,171.57,171.81,8036.24\n"
            "2024-08-01 00:01:00,171.8,171.97,171.74,171.86,3712.19\n"
        )
        config_manager = config_manager_from(valid_config, {"trading_settings": {"historical_data_file": str(csv)}})
        service = BacktestExchangeService(config_manager)

        with pytest.raises(DataFetchError) as raised:
            service.fetch_ohlcv("SOL/USDT", "1m", "2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z")

        assert str(raised.value) == (
            f"No candles for SOL/USDT between 2025-01-01T00:00:00Z and 2025-01-02T00:00:00Z in {csv}"
        )

    def test_an_empty_ccxt_range_raises_data_fetch_error(self, valid_config, monkeypatch):
        _capping_exchange(monkeypatch, "binance", 1000, 0)
        service = BacktestExchangeService(
            config_manager_from(valid_config, {"trading_settings": {"historical_data_file": None}})
        )

        with pytest.raises(
            DataFetchError, match=r"^No candles for BTC/USDT between 2024-01-01T00:00:00Z and 2024-01-02T00:00:00Z$"
        ):
            service.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z")

    @pytest.mark.parametrize(
        ("exchange_name", "max_per_call", "candle_count"),
        [("binance", 1000, 600), ("htx", 1000, 600), ("okx", 300, 400), ("myokx", 300, 400), ("binance", 100, 300)],
    )
    def test_every_candle_of_the_range_arrives_whatever_the_exchange_cap(
        self, valid_config, monkeypatch, exchange_name, max_per_call, candle_count
    ):
        _capping_exchange(monkeypatch, exchange_name, max_per_call, candle_count)
        service = BacktestExchangeService(
            config_manager_from(
                valid_config, {"exchange": {"name": exchange_name}, "trading_settings": {"historical_data_file": None}}
            )
        )
        end_ms = START_MS + candle_count * HOUR_MS

        df = service.fetch_ohlcv(
            "BTC/USDT", "1h", "2024-01-01T00:00:00Z", pd.Timestamp(end_ms, unit="ms").strftime("%Y-%m-%dT%H:%M:%SZ")
        )

        assert list(df.index) == [pd.Timestamp(candle[0], unit="ms") for candle in _hourly_candles(candle_count)]

    def test_a_range_of_one_instant_returns_its_candle(self, valid_config, monkeypatch):
        _capping_exchange(monkeypatch, "binance", 1000, 3)
        service = BacktestExchangeService(
            config_manager_from(valid_config, {"trading_settings": {"historical_data_file": None}})
        )

        df = service.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z")

        assert list(df.index) == [pd.Timestamp(START_MS, unit="ms")]

    def test_an_answer_that_does_not_move_forward_ends_the_fetch(self, valid_config, monkeypatch):
        exchange = Mock()
        exchange.parse8601.side_effect = [START_MS + 5 * HOUR_MS, START_MS + 10 * HOUR_MS]
        exchange.fetch_ohlcv.return_value = _hourly_candles(2)
        monkeypatch.setattr(ccxt, "binance", lambda: exchange)
        service = BacktestExchangeService(
            config_manager_from(valid_config, {"trading_settings": {"historical_data_file": None}})
        )
        service._is_pair_supported = Mock(return_value=True)
        service._is_timeframe_supported = Mock(return_value=True)

        with pytest.raises(DataFetchError, match=r"^No candles"):
            service.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01T05:00:00Z", "2024-01-01T10:00:00Z")

        exchange.fetch_ohlcv.assert_called_once()

    def _hourly_csv(self, tmp_path):
        csv = tmp_path / "hourly.csv"
        rows = [f"{ts},1,2,0.5,1.5,10" for ts in pd.date_range("2023-12-31 20:00", "2024-01-01 03:00", freq="1h")]
        csv.write_text("timestamp,open,high,low,close,volume\n" + "\n".join(rows) + "\n")
        return csv

    def test_an_offset_date_is_read_as_utc(self, tmp_path, valid_config):
        csv = self._hourly_csv(tmp_path)
        service = BacktestExchangeService(
            config_manager_from(valid_config, {"trading_settings": {"historical_data_file": str(csv)}})
        )

        df = service.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01T00:00:00+02:00", "2024-01-01T01:00:00+02:00")

        assert list(df.index) == [pd.Timestamp("2023-12-31 22:00"), pd.Timestamp("2023-12-31 23:00")]

    def test_a_naive_date_keeps_its_meaning(self, tmp_path, valid_config):
        csv = self._hourly_csv(tmp_path)
        service = BacktestExchangeService(
            config_manager_from(valid_config, {"trading_settings": {"historical_data_file": str(csv)}})
        )

        df = service.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01T00:00:00", "2024-01-01T01:00:00")

        assert list(df.index) == [pd.Timestamp("2024-01-01 00:00"), pd.Timestamp("2024-01-01 01:00")]

    @patch("pandas.read_csv")
    def test_load_ohlcv_file_not_found(self, mock_read_csv, config_manager):
        mock_read_csv.side_effect = FileNotFoundError("File not found")
        service = BacktestExchangeService(config_manager)

        with pytest.raises(HistoricalMarketDataFileNotFoundError, match="Failed to load OHLCV data from file"):
            service.fetch_ohlcv("BTC/USD", "1d", "2023-01-01", "2023-01-05")

    @patch("grid_trading_bot.core.services.backtest_exchange_service.ccxt.binance")
    @patch.object(BacktestExchangeService, "_is_pair_supported", return_value=True)
    def test_a_range_the_first_call_covers_takes_one_call(self, mock_is_pair_supported, mock_ccxt, config_manager):
        mock_exchange = mock_ccxt.return_value
        mock_exchange.timeframes = {"1d": "1d"}
        mock_exchange.fetch_ohlcv.return_value = [
            [1622505600000, 34000, 35000, 33000, 34500, 1000],  # Sample OHLCV data
            [1622592000000, 34500, 35500, 34000, 35000, 1200],
        ]
        mock_exchange.parse8601.side_effect = [1622505600000, 1622592000000]

        service = BacktestExchangeService(config_manager)
        service.historical_data_file = None
        df = service.fetch_ohlcv("BTC/USD", "1d", "2021-06-01", "2021-06-02")

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2, "Expected 2 rows of data in DataFrame"
        assert df.iloc[0]["close"] == 34500
        assert df.iloc[1]["close"] == 35000
        mock_exchange.fetch_ohlcv.assert_called_once()

    @patch("grid_trading_bot.core.services.backtest_exchange_service.ccxt.binance")
    @patch.object(BacktestExchangeService, "_is_pair_supported", return_value=True)
    def test_fetch_ohlcv_in_chunks(self, mock_is_pair_supported, mock_ccxt, config_manager):
        mock_exchange = mock_ccxt.return_value
        mock_exchange.timeframes = {"1h": "1h"}
        mock_exchange.fetch_ohlcv.side_effect = [
            [[1622505600000, 34000, 35000, 33000, 34500, 1000]],  # June 1, 2021
            [[1622592000000, 34500, 35500, 34000, 35000, 1200]],  # June 2, 2021
        ]
        mock_exchange.parse8601.side_effect = [1622505600000, 1622592000000]  # Start and end dates in ms

        service = BacktestExchangeService(config_manager)
        service.historical_data_file = None
        service._get_candle_limit = Mock(return_value=1)

        start_date = "2021-06-01T00:00:00Z"
        end_date = "2021-06-02T00:00:00Z"

        df = service.fetch_ohlcv("BTC/USD", "1h", start_date, end_date)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2, "Expected 2 rows of data in DataFrame"
        assert df.iloc[0]["close"] == 34500
        assert df.iloc[1]["close"] == 35000

    @patch("grid_trading_bot.core.services.backtest_exchange_service.time.sleep", return_value=None)
    @patch("grid_trading_bot.core.services.backtest_exchange_service.ccxt.binance")
    def test_fetch_with_retry(self, mock_ccxt, mock_sleep, config_manager):
        mock_exchange = mock_ccxt.return_value
        mock_exchange.timeframes = {"1d": "1d"}
        mock_exchange.fetch_ohlcv.side_effect = [
            ccxt.BaseError("Network Error"),
            [[1622505600000, 34000, 35000, 33000, 34500, 1000]],
            [],
        ]

        service = BacktestExchangeService(config_manager)

        df = service._fetch_ohlcv_in_chunks("BTC/USD", "1d", 1622505600000, 1622592000000, 1000)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 1, "Expected 1 row of data in DataFrame"
        mock_sleep.assert_called_once()

    @patch("grid_trading_bot.core.services.backtest_exchange_service.ccxt.binance")
    @patch.object(BacktestExchangeService, "_is_pair_supported", return_value=False)
    def test_unsupported_pair_error(self, mock_is_pair_supported, mock_ccxt, config_manager):
        mock_exchange = mock_ccxt.return_value
        mock_exchange.timeframes = {"1d": "1d"}  # Ensure timeframe is supported for this test

        service = BacktestExchangeService(config_manager)
        service.historical_data_file = None

        with pytest.raises(UnsupportedPairError):
            service.fetch_ohlcv("UNSUPPORTED/PAIR", "1d", "2021-06-01", "2021-06-02")

    @patch("grid_trading_bot.core.services.backtest_exchange_service.ccxt.binance")
    @patch.object(BacktestExchangeService, "_is_pair_supported", return_value=True)
    def test_unsupported_timeframe_error(self, mock_is_pair_supported, mock_ccxt, config_manager):
        mock_exchange = mock_ccxt.return_value
        mock_exchange.timeframes = {"1m": "1m"}

        service = BacktestExchangeService(config_manager)
        service.historical_data_file = None

        with pytest.raises(UnsupportedTimeframeError):
            service.fetch_ohlcv("BTC/USD", "5m", "2021-06-01", "2021-06-02")

    @pytest.mark.asyncio
    async def test_place_order_not_implemented(self, config_manager):
        service = BacktestExchangeService(config_manager)

        with pytest.raises(NotImplementedError, match="place_order is not used in backtesting"):
            await service.place_order("BTC/USD", "buy", "market", 1, 1000)

    @pytest.mark.asyncio
    async def test_get_balance_not_implemented(self, config_manager):
        service = BacktestExchangeService(config_manager)

        with pytest.raises(NotImplementedError, match="get_balance is not used in backtesting"):
            await service.get_balance()

    @pytest.mark.asyncio
    async def test_get_current_price_not_implemented(self, config_manager):
        service = BacktestExchangeService(config_manager)

        with pytest.raises(NotImplementedError, match="get_current_price is not used in backtesting"):
            await service.get_current_price("BTC/USD")

    @pytest.mark.asyncio
    async def test_cancel_order_not_implemented(self, config_manager):
        service = BacktestExchangeService(config_manager)

        with pytest.raises(NotImplementedError, match="cancel_order is not used in backtesting"):
            await service.cancel_order("order_id", "BTC/USD")

    @pytest.mark.asyncio
    async def test_get_exchange_status_not_implemented(self, config_manager):
        service = BacktestExchangeService(config_manager)

        with pytest.raises(NotImplementedError, match="get_exchange_status is not used in backtesting"):
            await service.get_exchange_status()

    @pytest.mark.asyncio
    async def test_close_connection(self, config_manager):
        service = BacktestExchangeService(config_manager)

        with patch.object(service, "logger") as mock_logger:
            await service.close_connection()
            mock_logger.info.assert_called_once_with("[BACKTEST] Closing WebSocket connection...")

    @pytest.mark.asyncio
    async def test_close_is_a_no_op(self, config_manager):
        service = BacktestExchangeService(config_manager)

        await service.close()

    @pytest.mark.asyncio
    async def test_fetch_order_by_client_id_not_implemented(self, config_manager):
        service = BacktestExchangeService(config_manager)

        with pytest.raises(NotImplementedError, match="fetch_order_by_client_id is not used in backtesting"):
            await service.fetch_order_by_client_id("g1", "BTC/USD")

    @pytest.mark.asyncio
    async def test_the_live_only_calls_are_not_used_in_backtesting(self, config_manager):
        service = BacktestExchangeService(config_manager)

        with pytest.raises(NotImplementedError, match="fetch_order is not used in backtesting"):
            await service.fetch_order("1", "BTC/USDT")
        with pytest.raises(NotImplementedError, match="fetch_recent_ohlcv is not used in backtesting"):
            await service.fetch_recent_ohlcv("BTC/USDT", "1h", 10)
        with pytest.raises(NotImplementedError, match="listen_to_ticker_updates is not used in backtesting"):
            await service.listen_to_ticker_updates("BTC/USDT", AsyncMock(), 3)
        assert service.last_price is None

    def test_rounding_keeps_the_values(self, config_manager):
        service = BacktestExchangeService(config_manager)

        assert service.round_amount("BTC/USD", 0.123456789) == 0.123456789
        assert service.round_price("BTC/USD", 100.4567) == 100.4567
