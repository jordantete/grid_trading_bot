from unittest.mock import Mock, patch

import pytest

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.services.backtest_exchange_service import BacktestExchangeService
from grid_trading_bot.core.services.exchange_service_factory import ExchangeServiceFactory
from grid_trading_bot.core.services.live_exchange_service import LiveExchangeService


class TestExchangeServiceFactory:
    @pytest.fixture
    def config_manager(self):
        config_manager = Mock(spec=ConfigManager)
        config_manager.get_trading_mode.return_value = TradingMode.LIVE
        config_manager.get_exchange_name.return_value = "binance"
        return config_manager

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_create_live_exchange_service_with_env_vars(self, mock_getattr, mock_ccxtpro, config_manager, monkeypatch):
        monkeypatch.setenv("EXCHANGE_API_KEY", "test_api_key")
        monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test_secret_key")

        mock_exchange_instance = Mock()
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_getattr.return_value = mock_ccxtpro.binance

        service = ExchangeServiceFactory.create_exchange_service(config_manager, TradingMode.LIVE)

        assert isinstance(service, LiveExchangeService), "Expected a LiveExchangeService instance"
        mock_getattr.assert_called_once_with(mock_ccxtpro, "binance")
        mock_ccxtpro.binance.assert_called_once_with(
            {
                "apiKey": "test_api_key",
                "secret": "test_secret_key",
                "enableRateLimit": True,
            },
        )

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_create_backtest_exchange_service(self, mock_getattr, mock_ccxtpro, config_manager):
        config_manager.get_trading_mode.return_value = TradingMode.BACKTEST
        service = ExchangeServiceFactory.create_exchange_service(config_manager, TradingMode.BACKTEST)
        assert isinstance(service, BacktestExchangeService), "Expected a BacktestExchangeService instance"

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_create_paper_trading_exchange_service(self, mock_getattr, mock_ccxtpro, config_manager, monkeypatch):
        monkeypatch.setenv("EXCHANGE_API_KEY", "test_api_key")
        monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test_secret_key")

        mock_exchange_instance = Mock()
        mock_exchange_instance.urls = {"api": "https://api.binance.com/api"}
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_getattr.return_value = mock_ccxtpro.binance

        service = ExchangeServiceFactory.create_exchange_service(config_manager, TradingMode.PAPER_TRADING)

        assert isinstance(service, LiveExchangeService), "Expected a LiveExchangeService instance"
        assert service.is_paper_trading_activated is True
        assert mock_exchange_instance.urls["api"] == "https://testnet.binance.vision/api"

    def test_invalid_trading_mode(self, config_manager):
        config_manager.get_trading_mode.return_value = "invalid_mode"
        with pytest.raises(ValueError, match="Unsupported trading mode: invalid_mode"):
            ExchangeServiceFactory.create_exchange_service(config_manager, "invalid_mode")
