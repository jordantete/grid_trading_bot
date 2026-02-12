from unittest.mock import Mock, patch

import pandas as pd
import pytest

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.services.backtest_exchange_service import BacktestExchangeService
from grid_trading_bot.core.services.exceptions import (
    HistoricalMarketDataFileNotFoundError,
    UnsupportedExchangeError,
    UnsupportedPairError,
    UnsupportedTimeframeError,
)


class TestBacktestExchangeService:
    @pytest.fixture
    def config_manager(self):
        mock_config = Mock(spec=ConfigManager)
        mock_config.get_exchange_name.return_value = "binance"
        mock_config.get_historical_data_file.return_value = "data/test_data.csv"
        return mock_config

    @pytest.fixture
    def backtest_service(self, config_manager):
        return BacktestExchangeService(config_manager)

    @patch("grid_trading_bot.core.services.backtest_exchange_service.ccxt.binance")
    def test_initialization_supported_exchange(self, mock_ccxt, config_manager):
        service = BacktestExchangeService(config_manager)
        mock_ccxt.assert_called_once()
        assert service.exchange_name == "binance", "Expected exchange name to be 'binance'"

    def test_initialization_unsupported_exchange(self, config_manager):
        config_manager.get_exchange_name.return_value = "unsupported_exchange"
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
        pd.testing.assert_frame_equal(result, mock_data)

    @patch("pandas.read_csv")
    def test_load_ohlcv_file_not_found(self, mock_read_csv, config_manager):
        mock_read_csv.side_effect = FileNotFoundError("File not found")
        service = BacktestExchangeService(config_manager)

        with pytest.raises(HistoricalMarketDataFileNotFoundError, match="Failed to load OHLCV data from file"):
            service.fetch_ohlcv("BTC/USD", "1d", "2023-01-01", "2023-01-05")

    @patch("grid_trading_bot.core.services.backtest_exchange_service.ccxt.binance")
    @patch.object(BacktestExchangeService, "_is_pair_supported", return_value=True)
    def test_fetch_ohlcv_single_batch(self, mock_is_pair_supported, mock_ccxt, config_manager):
        # Mock the exchange instance and returned data
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
            Exception("Network Error"),
            [[1622505600000, 34000, 35000, 33000, 34500, 1000]],
        ]

        service = BacktestExchangeService(config_manager)

        df = service._fetch_ohlcv_single_batch("BTC/USD", "1d", 1622505600000, 1622592000000)

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
