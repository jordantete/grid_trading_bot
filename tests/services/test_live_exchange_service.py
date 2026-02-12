import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import ccxt
import pytest

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.services.exceptions import (
    DataFetchError,
    MissingEnvironmentVariableError,
    OrderCancellationError,
    UnsupportedExchangeError,
)
from grid_trading_bot.core.services.live_exchange_service import LiveExchangeService


class TestLiveExchangeService:
    @pytest.fixture
    def config_manager(self):
        config_manager = Mock(spec=ConfigManager)
        config_manager.get_exchange_name.return_value = "binance"
        config_manager.get_trading_mode.return_value = TradingMode.LIVE
        return config_manager

    @pytest.fixture
    def mock_exchange_instance(self):
        exchange = AsyncMock()
        exchange.fetch_balance.return_value = {"total": {"USD": 1000}}
        exchange.fetch_ticker.return_value = {"last": 50000.0}
        exchange.fetch_order.return_value = {"status": "closed"}
        exchange.cancel_order.return_value = {"status": "canceled"}
        return exchange

    @pytest.fixture
    def setup_env_vars(self, monkeypatch):
        monkeypatch.setenv("EXCHANGE_API_KEY", "test_api_key")
        monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test_secret_key")

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_initialization_with_env_vars(self, mock_getattr, mock_ccxtpropro, config_manager, setup_env_vars):
        mock_exchange_instance = Mock()
        mock_ccxtpropro.binance.return_value = mock_exchange_instance
        mock_getattr.return_value = mock_ccxtpropro.binance

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)
        assert isinstance(service, LiveExchangeService)
        assert service.exchange_name == "binance"
        assert service.api_key == "test_api_key"
        assert service.secret_key == "test_secret_key"  # noqa: S105
        assert service.exchange == mock_exchange_instance
        assert service.exchange.enableRateLimit, "Expected rate limiting to be enabled for live mode"

    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_missing_secret_key_raises_error(self, config_manager, monkeypatch):
        monkeypatch.delenv("EXCHANGE_SECRET_KEY", raising=False)
        monkeypatch.setenv("EXCHANGE_API_KEY", "test_api_key")

        with pytest.raises(
            MissingEnvironmentVariableError,
            match="Missing required environment variable: EXCHANGE_SECRET_KEY",
        ):
            LiveExchangeService(config_manager, is_paper_trading_activated=False)

    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_missing_api_key_raises_error(self, config_manager, monkeypatch):
        monkeypatch.delenv("EXCHANGE_API_KEY", raising=False)
        monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test_secret_key")

        with pytest.raises(
            MissingEnvironmentVariableError,
            match="Missing required environment variable: EXCHANGE_API_KEY",
        ):
            LiveExchangeService(config_manager, is_paper_trading_activated=False)

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_sandbox_mode_initialization(self, mock_getattr, mock_ccxtpropro, config_manager, setup_env_vars):
        config_manager.get_trading_mode.return_value = TradingMode.PAPER_TRADING

        mock_exchange_instance = MagicMock()
        mock_exchange_instance.urls = {"api": "https://api.binance.com"}  # Initial URL setup
        mock_ccxtpropro.binance.return_value = mock_exchange_instance
        mock_getattr.return_value = mock_ccxtpropro.binance

        service = LiveExchangeService(config_manager, is_paper_trading_activated=True)

        assert service.is_paper_trading_activated is True
        expected_sandbox_url = "https://testnet.binance.vision/api"
        assert mock_exchange_instance.urls["api"] == expected_sandbox_url, "Sandbox URL not correctly set for Binance."

    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_unsupported_exchange_raises_error(self, mock_getattr, config_manager, setup_env_vars):
        config_manager.get_exchange_name.return_value = "unsupported_exchange"
        mock_getattr.side_effect = AttributeError

        with pytest.raises(UnsupportedExchangeError, match="The exchange 'unsupported_exchange' is not supported."):
            LiveExchangeService(config_manager, is_paper_trading_activated=False)

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_place_order_successful(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)
        await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0)

        mock_exchange_instance.create_order.assert_called_once_with("BTC/USD", "limit", "buy", 1, 50000.0)

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_place_order_unexpected_error(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.create_order.side_effect = Exception("Unexpected error")
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(DataFetchError, match="Unexpected error placing order"):
            await service.place_order("BTC/USD", "market", "buy", 1, 50000.0)
        mock_exchange_instance.create_order.assert_awaited_once_with("BTC/USD", "market", "buy", 1, 50000.0)

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_get_current_price_successful(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)
        result = await service.get_current_price("BTC/USD")

        assert result == 50000.0
        mock_exchange_instance.fetch_ticker.assert_called_once_with("BTC/USD")

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_cancel_order_successful(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)
        result = await service.cancel_order("order123", "BTC/USD")

        assert result["status"] == "canceled"
        mock_exchange_instance.cancel_order.assert_called_once_with("order123", "BTC/USD")

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_cancel_order_unexpected_error(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.cancel_order.side_effect = Exception("Unexpected error")
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(
            OrderCancellationError,
            match="Unexpected error while canceling order order123: Unexpected error",
        ):
            await service.cancel_order("order123", "BTC/USD")
        mock_exchange_instance.cancel_order.assert_awaited_once_with("order123", "BTC/USD")

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_cancel_order_network_error(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.cancel_order.side_effect = ccxt.NetworkError("Network issue")

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(OrderCancellationError, match="Network error while canceling order order123: Network issue"):
            await service.cancel_order("order123", "BTC/USD")
        mock_exchange_instance.cancel_order.assert_awaited_once_with("order123", "BTC/USD")

    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    def test_fetch_ohlcv_not_implemented(self, mock_ccxtpro, mock_getattr, setup_env_vars, config_manager):
        mock_exchange_instance = Mock()
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_getattr.return_value = mock_ccxtpro.binance

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(NotImplementedError, match="fetch_ohlcv is not used in live or paper trading mode."):
            service.fetch_ohlcv("BTC/USD", "1m", "start_date", "end_date")

    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @pytest.mark.asyncio
    async def test_get_exchange_status_ok(self, mock_ccxtpro, mock_getattr, setup_env_vars, config_manager):
        mock_exchange_instance = Mock()
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_getattr.return_value = mock_ccxtpro.binance

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        service.exchange.fetch_status = AsyncMock(
            return_value={
                "status": "ok",
                "updated": 1622505600000,
                "eta": None,
                "url": "https://status.exchange.com",
                "info": "All systems operational.",
            },
        )

        result = await service.get_exchange_status()

        assert result == {
            "status": "ok",
            "updated": 1622505600000,
            "eta": None,
            "url": "https://status.exchange.com",
            "info": "All systems operational.",
        }

    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @pytest.mark.asyncio
    async def test_get_exchange_status_unsupported(self, mock_ccxtpro, mock_getattr, setup_env_vars, config_manager):
        mock_exchange_instance = Mock()
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_getattr.return_value = mock_ccxtpro.binance

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)
        service.exchange.fetch_status.side_effect = AttributeError

        result = await service.get_exchange_status()

        assert result == {
            "status": "unsupported",
            "info": "fetch_status not supported by this exchange.",
        }

    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @pytest.mark.asyncio
    async def test_get_exchange_status_error(self, mock_ccxtpro, mock_getattr, setup_env_vars, config_manager):
        mock_exchange_instance = Mock()
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_getattr.return_value = mock_ccxtpro.binance

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)
        service.exchange.fetch_status.side_effect = Exception("Network error")

        result = await service.get_exchange_status()

        assert result["status"] == "error"
        assert "Failed to fetch exchange status" in result["info"]
        assert "Network error" in result["info"]

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_close_connection(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        service.connection_active = True

        await service.close_connection()
        assert service.connection_active is False

    @pytest.fixture
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def setup_websocket_test(self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars, mock_exchange_instance):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)
        service.connection_active = True
        on_ticker_update = AsyncMock()
        return service, mock_exchange_instance, on_ticker_update

    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    async def test_subscribe_to_ticker_updates_success(self, setup_websocket_test):
        service, mock_exchange_instance, on_ticker_update = setup_websocket_test
        mock_exchange_instance.watch_ticker = AsyncMock(
            side_effect=[
                {"last": 50000.0},
                asyncio.CancelledError(),
            ],
        )

        await service._subscribe_to_ticker_updates("BTC/USD", on_ticker_update, 0.1)

        on_ticker_update.assert_awaited_once_with(50000.0)
        assert not service.connection_active

    @pytest.mark.asyncio
    @pytest.mark.timeout(2)
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @patch("grid_trading_bot.core.services.live_exchange_service.asyncio.sleep", new_callable=AsyncMock)
    async def test_subscribe_to_ticker_updates_network_error(
        self,
        mock_sleep,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.watch_ticker = AsyncMock(
            side_effect=[
                ccxt.NetworkError("Network issue"),
                asyncio.CancelledError(),
            ],
        )

        on_ticker_update = AsyncMock()
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)
        service.connection_active = True

        with patch.object(service.logger, "error") as mock_logger_error:
            await service._subscribe_to_ticker_updates("BTC/USD", on_ticker_update, 0.1, max_retries=1)

            mock_logger_error.assert_any_call(
                "Error connecting to WebSocket for BTC/USD: Network issue. Retrying in 5 seconds (1/1).",
            )

            on_ticker_update.assert_not_awaited()

        assert not service.connection_active
        assert mock_exchange_instance.watch_ticker.await_count == 1

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_subscribe_to_ticker_updates_max_retries_exceeded(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.watch_ticker = AsyncMock(side_effect=ccxt.NetworkError("Network issue"))

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)
        service.connection_active = True
        on_ticker_update = AsyncMock()

        await service._subscribe_to_ticker_updates("BTC/USD", on_ticker_update, 0.1, max_retries=2)

        assert not service.connection_active
        assert mock_exchange_instance.watch_ticker.await_count == 2

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_subscribe_to_ticker_updates_close_error(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.watch_ticker = AsyncMock(side_effect=asyncio.CancelledError())
        mock_exchange_instance.close = AsyncMock(side_effect=Exception("Close error"))

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)
        service.connection_active = True
        on_ticker_update = AsyncMock()

        with patch.object(service.logger, "error") as mock_logger_error:
            await service._subscribe_to_ticker_updates("BTC/USD", on_ticker_update, 0.1)
            mock_logger_error.assert_any_call("Error while closing WebSocket connection: Close error", exc_info=True)

    @pytest.mark.parametrize(
        ("exchange_name", "expected_url"),
        [
            ("binance", "https://testnet.binance.vision/api"),
            ("kraken", "https://api.demo-futures.kraken.com"),
            ("bitmex", "https://testnet.bitmex.com"),
            ("bybit", None),  # bybit uses set_sandbox_mode
            ("unknown", None),
        ],
    )
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_enable_sandbox_mode_all_exchanges(
        self,
        mock_getattr,
        mock_ccxtpro,
        exchange_name,
        expected_url,
        config_manager,
        setup_env_vars,
    ):
        config_manager.get_exchange_name.return_value = exchange_name
        mock_exchange_instance = Mock()
        mock_exchange_instance.urls = {"api": "default_url"}
        mock_exchange_instance.set_sandbox_mode = Mock()
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_getattr.return_value = mock_ccxtpro.binance

        LiveExchangeService(config_manager, is_paper_trading_activated=True)

        if exchange_name == "bybit":
            mock_exchange_instance.set_sandbox_mode.assert_called_once_with(True)
        elif expected_url:
            assert mock_exchange_instance.urls["api"] == expected_url
        else:
            assert mock_exchange_instance.urls["api"] == "default_url"

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_place_order_network_error(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.create_order = AsyncMock(side_effect=ccxt.NetworkError("Network error"))

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(DataFetchError, match="Network issue occurred while placing order: Network error"):
            await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0)

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_fetch_order_network_error(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.fetch_order = AsyncMock(side_effect=ccxt.NetworkError("Network error"))

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(DataFetchError, match="Network issue occurred while fetching order status: Network error"):
            await service.fetch_order("BTC/USD", "123")
