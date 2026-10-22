import asyncio
import dataclasses
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import ccxt
import pytest
from tests.fakes.config import config_manager_from

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.services.circuit_breaker import CircuitState
from grid_trading_bot.core.services.exceptions import (
    DataFetchError,
    MissingEnvironmentVariableError,
    OrderCancellationError,
    OrderNotFoundError,
    OrderNotPlacedError,
    OrderOutcomeUnknownError,
    OrderRejectedError,
    PostOnlyRejectedError,
    UnsupportedExchangeError,
)
from grid_trading_bot.core.services.live_exchange_service import LiveExchangeService


def _with_exchange(config_manager, **fields):
    exchange = dataclasses.replace(config_manager.config.exchange, **fields)
    config_manager.config = dataclasses.replace(config_manager.config, exchange=exchange)


class TestLiveExchangeService:
    @pytest.fixture
    def config_manager(self, valid_config):
        return config_manager_from(
            valid_config, {"exchange": {"name": "binance", "trading_mode": "live", "post_only": False}}
        )

    @pytest.fixture
    def mock_exchange_instance(self):
        exchange = AsyncMock()
        exchange.fetch_balance.return_value = {"total": {"USD": 1000}}
        exchange.fetch_ticker.return_value = {"last": 50000.0}
        exchange.fetch_order.return_value = {"status": "closed"}
        exchange.cancel_order.return_value = {"status": "canceled"}
        exchange.amount_to_precision = Mock(side_effect=lambda pair, amount: str(amount))
        exchange.price_to_precision = Mock(side_effect=lambda pair, price: str(price))
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
        _with_exchange(config_manager, trading_mode=TradingMode.PAPER_TRADING)

        mock_exchange_instance = MagicMock()
        mock_exchange_instance.urls = {"api": "https://api.binance.com"}  # Initial URL setup
        mock_exchange_instance.has = {"sandbox": True}
        mock_ccxtpropro.binance.return_value = mock_exchange_instance
        mock_getattr.return_value = mock_ccxtpropro.binance

        service = LiveExchangeService(config_manager, is_paper_trading_activated=True)

        assert service.is_paper_trading_activated is True
        mock_exchange_instance.set_sandbox_mode.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_a_paper_binance_signs_spot_requests_on_the_testnet(self, config_manager, setup_env_vars):
        service = LiveExchangeService(config_manager, is_paper_trading_activated=True)
        try:
            api = service.exchange.urls["api"]

            assert isinstance(api, dict)
            assert api["public"].startswith("https://testnet.binance.vision/")
            assert api["private"].startswith("https://testnet.binance.vision/")
            assert service.exchange.sign("ping", "public", "GET", {})["url"] == (
                "https://testnet.binance.vision/api/v3/ping"
            )
        finally:
            await service.exchange.close()

    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_unsupported_exchange_raises_error(self, mock_getattr, config_manager, setup_env_vars):
        _with_exchange(config_manager, name="unsupported_exchange")
        mock_getattr.side_effect = AttributeError

        with pytest.raises(UnsupportedExchangeError, match=r"The exchange 'unsupported_exchange' is not supported."):
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

        mock_exchange_instance.create_order.assert_called_once_with("BTC/USD", "limit", "buy", 1, 50000.0, params={})

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_place_order_sends_the_client_order_id(
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
        await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0, client_order_id="g3f2a9c1d0123456789abcdef")

        mock_exchange_instance.create_order.assert_awaited_once_with(
            "BTC/USD", "limit", "buy", 1, 50000.0, params={"clientOrderId": "g3f2a9c1d0123456789abcdef"}
        )

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            ccxt.RequestTimeout("timed out"),
            ccxt.ExchangeNotAvailable("503"),
            ccxt.DuplicateOrderId("duplicate"),
            ccxt.OperationFailed("-1006 Execution status unknown"),
            ccxt.BadResponse("bad body"),
            ccxt.NullResponse("empty body"),
            ccxt.ExchangeError("body cut off"),
        ],
    )
    async def test_a_placement_whose_answer_may_be_lost_has_an_unknown_outcome(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
        error,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.create_order.side_effect = error
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(OrderOutcomeUnknownError):
            await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0, client_order_id="g1")

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message",
        [
            'binance {"code":-2010,"msg":"Duplicate order sent."}',
            'okx {"code":"1","data":[{"sCode":"51016","sMsg":"Duplicated client order ID"}]}',
            'binanceusdm {"code":-4116,"msg":"ClientOrderId is duplicated."}',
        ],
    )
    async def test_a_duplicate_client_id_reported_as_invalid_order_has_an_unknown_outcome(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
        message,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.create_order.side_effect = ccxt.InvalidOrder(message)
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        for _ in range(6):
            with pytest.raises(OrderOutcomeUnknownError):
                await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0, client_order_id="g1")
        assert service.circuit_breaker.state == CircuitState.CLOSED

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_duplicate_wording_without_a_client_id_stays_a_refusal(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.create_order.side_effect = ccxt.InvalidOrder("Duplicate order sent.")
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(OrderRejectedError):
            await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0)

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            ccxt.InsufficientFunds("balance too low"),
            ccxt.InvalidOrder("Invalid price precision"),
            ccxt.BadSymbol("no such market"),
        ],
    )
    async def test_a_business_refusal_is_an_order_rejection(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
        error,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.create_order.side_effect = error
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(OrderRejectedError) as raised:
            await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0)
        assert not isinstance(raised.value, PostOnlyRejectedError | DataFetchError)

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_an_amount_under_the_precision_is_refused_before_sending(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.amount_to_precision = Mock(
            side_effect=ccxt.InvalidOrder("amount must be greater than minimum amount precision")
        )
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(OrderRejectedError):
            await service.place_order("BTC/USD", "limit", "buy", 0.000001, 50000.0)
        mock_exchange_instance.create_order.assert_not_awaited()

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_rounding_follows_the_exchange_precision(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.amount_to_precision = Mock(return_value="0.123")
        mock_exchange_instance.price_to_precision = Mock(return_value="100.45")
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        assert service.round_amount("SOL/USDT", 0.12345678) == 0.123
        assert service.round_price("SOL/USDT", 100.4567) == 100.45
        mock_exchange_instance.amount_to_precision.assert_called_once_with("SOL/USDT", 0.12345678)
        mock_exchange_instance.price_to_precision.assert_called_once_with("SOL/USDT", 100.4567)

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_an_order_is_looked_up_by_its_client_order_id(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.fetch_order = AsyncMock(return_value={"id": "42", "status": "open"})
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        found = await service.fetch_order_by_client_id("g1", "BTC/USD")

        assert found == {"id": "42", "status": "open"}
        mock_exchange_instance.fetch_order.assert_awaited_once_with(None, "BTC/USD", params={"clientOrderId": "g1"})

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_an_unknown_client_order_id_is_not_found(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.fetch_order = AsyncMock(side_effect=ccxt.OrderNotFound("Order does not exist."))
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(OrderNotFoundError, match="g1"):
            await service.fetch_order_by_client_id("g1", "BTC/USD")

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_a_failed_client_order_id_lookup_is_a_fetch_error(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.fetch_order = AsyncMock(side_effect=ccxt.NetworkError("down"))
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(DataFetchError) as raised:
            await service.fetch_order_by_client_id("g1", "BTC/USD")
        assert not isinstance(raised.value, OrderNotFoundError)

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_cancelling_an_order_the_exchange_no_longer_knows_raises_not_found(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.cancel_order.side_effect = ccxt.OrderNotFound("Unknown order sent.")
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(OrderNotFoundError, match="order123"):
            await service.cancel_order("order123", "BTC/USD")

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
        mock_exchange_instance.create_order.side_effect = ccxt.BaseError("Exchange error")
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(OrderOutcomeUnknownError, match="outcome is unknown"):
            await service.place_order("BTC/USD", "market", "buy", 1, 50000.0)
        mock_exchange_instance.create_order.assert_awaited_once_with("BTC/USD", "market", "buy", 1, 50000.0, params={})

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
        mock_exchange_instance.cancel_order.side_effect = ccxt.BaseError("Exchange error")
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(
            OrderCancellationError,
            match="Exchange error while canceling order order123: Exchange error",
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

        with pytest.raises(NotImplementedError, match=r"fetch_ohlcv is not used in live or paper trading mode."):
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
    async def test_subscribe_to_ticker_updates_skips_invalid_price(self, setup_websocket_test):
        service, mock_exchange_instance, on_ticker_update = setup_websocket_test
        mock_exchange_instance.watch_ticker = AsyncMock(
            side_effect=[
                {"last": None},
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
        service.websocket_max_retries = 1

        with patch.object(service.logger, "error") as mock_logger_error:
            with pytest.raises(DataFetchError, match="failed after 1 retries"):
                await service._subscribe_to_ticker_updates("BTC/USD", on_ticker_update, 0.1)

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
        service.websocket_max_retries = 2
        on_ticker_update = AsyncMock()

        with pytest.raises(DataFetchError, match="failed after 2 retries"):
            await service._subscribe_to_ticker_updates("BTC/USD", on_ticker_update, 0.1)

        assert not service.connection_active
        assert mock_exchange_instance.watch_ticker.await_count == 2

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_the_ticker_loop_never_closes_the_exchange(
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
        mock_exchange_instance.close = AsyncMock()

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)
        service.connection_active = True
        on_ticker_update = AsyncMock()

        await service._subscribe_to_ticker_updates("BTC/USD", on_ticker_update, 0.1)

        mock_exchange_instance.close.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exchange_name", ["binance", "bybit", "okx"])
    async def test_paper_mode_switches_a_sandbox_exchange_to_its_test_environment(
        self, config_manager, setup_env_vars, exchange_name
    ):
        _with_exchange(config_manager, name=exchange_name)

        service = LiveExchangeService(config_manager, is_paper_trading_activated=True)
        try:
            assert service.exchange.isSandboxModeEnabled is True
        finally:
            await service.exchange.close()

    @pytest.mark.asyncio
    async def test_paper_mode_on_okx_trades_in_its_demo_environment(self, config_manager, setup_env_vars):
        _with_exchange(config_manager, name="okx")

        service = LiveExchangeService(config_manager, is_paper_trading_activated=True)
        try:
            assert service.exchange.headers["x-simulated-trading"] == "1"
        finally:
            await service.exchange.close()

    @pytest.mark.asyncio
    async def test_paper_mode_on_an_exchange_without_a_sandbox_is_refused(self, config_manager, setup_env_vars):
        _with_exchange(config_manager, name="kraken")

        with pytest.raises(
            UnsupportedExchangeError, match=r"^kraken has no test environment; paper trading refused\.$"
        ):
            LiveExchangeService(config_manager, is_paper_trading_activated=True)

    @pytest.mark.asyncio
    async def test_live_mode_keeps_an_exchange_without_a_sandbox_on_its_api(self, config_manager, setup_env_vars):
        _with_exchange(config_manager, name="kraken")

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)
        try:
            assert service.exchange.isSandboxModeEnabled is False
        finally:
            await service.exchange.close()

    @pytest.mark.asyncio
    async def test_paper_mode_on_a_name_ccxt_does_not_know_is_an_unsupported_exchange(
        self, config_manager, setup_env_vars
    ):
        _with_exchange(config_manager, name="notanexchange")

        with pytest.raises(UnsupportedExchangeError, match=r"The exchange 'notanexchange' is not supported\."):
            LiveExchangeService(config_manager, is_paper_trading_activated=True)

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

        with pytest.raises(OrderOutcomeUnknownError, match="Network issue occurred while placing order: Network error"):
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

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_fetch_order_reports_an_unknown_order_as_not_found(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.fetch_order = AsyncMock(side_effect=ccxt.OrderNotFound("binance Order does not exist."))

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(OrderNotFoundError, match="Order 123 not found on BTC/USD"):
            await service.fetch_order("123", "BTC/USD")

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_unknown_orders_do_not_open_the_circuit_breaker(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.fetch_order = AsyncMock(side_effect=ccxt.OrderNotFound("binance Order does not exist."))

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        for _ in range(6):
            with pytest.raises(OrderNotFoundError):
                await service.fetch_order("123", "BTC/USD")

        assert service.circuit_breaker.state == CircuitState.CLOSED

    # ── _validate_price ────────────────────────────────────────────────

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_validate_price_valid(self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars):
        mock_ccxtpro.binance.return_value = Mock()
        mock_getattr.return_value = mock_ccxtpro.binance
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        result = service._validate_price(50000.0, "BTC/USD")
        assert result == 50000.0
        assert service._last_known_price == 50000.0

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_validate_price_none(self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars):
        mock_ccxtpro.binance.return_value = Mock()
        mock_getattr.return_value = mock_ccxtpro.binance
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        assert service._validate_price(None, "BTC/USD") is None

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_validate_price_nan(self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars):
        mock_ccxtpro.binance.return_value = Mock()
        mock_getattr.return_value = mock_ccxtpro.binance
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        assert service._validate_price(float("nan"), "BTC/USD") is None

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_validate_price_negative(self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars):
        mock_ccxtpro.binance.return_value = Mock()
        mock_getattr.return_value = mock_ccxtpro.binance
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        assert service._validate_price(-100.0, "BTC/USD") is None

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_validate_price_zero(self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars):
        mock_ccxtpro.binance.return_value = Mock()
        mock_getattr.return_value = mock_ccxtpro.binance
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        assert service._validate_price(0.0, "BTC/USD") is None

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_validate_price_infinity(self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars):
        mock_ccxtpro.binance.return_value = Mock()
        mock_getattr.return_value = mock_ccxtpro.binance
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        assert service._validate_price(float("inf"), "BTC/USD") is None

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_validate_price_excessive_deviation(self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars):
        mock_ccxtpro.binance.return_value = Mock()
        mock_getattr.return_value = mock_ccxtpro.binance
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        # Set a known price first
        service._last_known_price = 50000.0

        # 51% deviation should be rejected
        assert service._validate_price(100000.0, "BTC/USD") is None

        # 40% deviation should be accepted
        result = service._validate_price(70000.0, "BTC/USD")
        assert result == 70000.0

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_last_price_follows_accepted_prices_only(self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars):
        mock_ccxtpro.binance.return_value = Mock()
        mock_getattr.return_value = mock_ccxtpro.binance
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        assert service.last_price is None
        service._validate_price(50000.0, "BTC/USD")
        service._validate_price(100000.0, "BTC/USD")  # rejected: 100 % deviation

        assert service.last_price == 50000.0

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_get_current_price_invalid_raises_error(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.fetch_ticker.return_value = {"last": None}

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        with pytest.raises(DataFetchError, match="Invalid price received"):
            await service.get_current_price("BTC/USD")

    # ── load_exchange_markets / get_market_constraints ──────────────────

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_load_exchange_markets_success(
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
        mock_exchange_instance.load_markets = AsyncMock()
        mock_exchange_instance.markets = {"SOL/USDT": {"limits": {"amount": {"min": 0.01}, "cost": {"min": 5.0}}}}

        await service.load_exchange_markets("SOL/USDT")

        mock_exchange_instance.load_markets.assert_awaited_once()

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_load_exchange_markets_unknown_pair_raises(
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
        mock_exchange_instance.load_markets = AsyncMock()
        mock_exchange_instance.markets = {"BTC/USDT": {}}

        with pytest.raises(DataFetchError, match="SOL/USDT"):
            await service.load_exchange_markets("SOL/USDT")

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_get_market_constraints_reads_limits(
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
        mock_exchange_instance.markets = {"SOL/USDT": {"limits": {"amount": {"min": 0.01}, "cost": {"min": 5.0}}}}

        constraints = service.get_market_constraints("SOL/USDT")

        assert constraints.min_amount == 0.01
        assert constraints.min_cost == 5.0

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def test_get_market_constraints_without_markets_raises(
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
        mock_exchange_instance.markets = {}

        with pytest.raises(DataFetchError):
            service.get_market_constraints("SOL/USDT")

    # ── place_order precision ────────────────────────────────────────────

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_place_order_applies_precision(
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
        mock_exchange_instance.amount_to_precision = Mock(return_value="0.123")
        mock_exchange_instance.price_to_precision = Mock(return_value="100.45")
        mock_exchange_instance.create_order = AsyncMock(return_value={"id": "1"})

        await service.place_order("SOL/USDT", "limit", "buy", 0.12345678, 100.4567)

        mock_exchange_instance.create_order.assert_awaited_once_with(
            "SOL/USDT",
            "limit",
            "buy",
            0.123,
            100.45,
            params={},
        )


class TestLiveExchangeServiceCircuitBreaker:
    @pytest.fixture
    def config_manager(self, valid_config):
        return config_manager_from(
            valid_config,
            {
                "exchange": {"name": "binance", "trading_mode": "live", "post_only": False},
                "execution": {"circuit_breaker_failure_threshold": 2},
            },
        )

    @pytest.fixture
    def mock_exchange_instance(self):
        exchange = AsyncMock()
        exchange.fetch_balance.return_value = {"total": {"USD": 1000}}
        exchange.fetch_ticker.return_value = {"last": 50000.0}
        exchange.fetch_order.return_value = {"status": "closed"}
        exchange.cancel_order.return_value = {"status": "canceled"}
        exchange.amount_to_precision = Mock(side_effect=lambda pair, amount: str(amount))
        exchange.price_to_precision = Mock(side_effect=lambda pair, price: str(price))
        return exchange

    @pytest.fixture
    def setup_env_vars(self, monkeypatch):
        monkeypatch.setenv("EXCHANGE_API_KEY", "test_api_key")
        monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test_secret_key")

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_circuit_breaker_opens_after_consecutive_failures(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.fetch_balance.side_effect = ccxt.NetworkError("API down")

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        # 2 failures should trip the breaker (threshold=2)
        for _ in range(2):
            with pytest.raises(DataFetchError, match="Error fetching balance"):
                await service.get_balance()

        assert service.circuit_breaker.state == CircuitState.OPEN

        # Next call should be rejected by circuit breaker
        with pytest.raises(DataFetchError, match="Circuit breaker open"):
            await service.get_balance()

        # Underlying exchange method should not have been called a 3rd time
        assert mock_exchange_instance.fetch_balance.await_count == 2

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_circuit_breaker_shared_across_methods(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.fetch_balance.side_effect = ccxt.NetworkError("API down")
        mock_exchange_instance.fetch_ticker.side_effect = ccxt.NetworkError("API down")

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        # 1 failure from get_balance
        with pytest.raises(DataFetchError):
            await service.get_balance()

        # 1 failure from get_current_price — should trip the shared breaker
        with pytest.raises(DataFetchError):
            await service.get_current_price("BTC/USD")

        assert service.circuit_breaker.state == CircuitState.OPEN

        # All wrapped methods should now fail with circuit breaker open
        with pytest.raises(DataFetchError, match="Circuit breaker open"):
            await service.get_balance()

        with pytest.raises(DataFetchError, match="Circuit breaker open"):
            await service.get_current_price("BTC/USD")

        with pytest.raises(OrderNotPlacedError, match="Circuit breaker open"):
            await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0)

        with pytest.raises(DataFetchError, match="Circuit breaker open"):
            await service.fetch_order("order123", "BTC/USD")

        with pytest.raises(OrderCancellationError, match="Circuit breaker open"):
            await service.cancel_order("order123", "BTC/USD")

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_circuit_breaker_cancel_order_open_raises_cancellation_error(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.cancel_order.side_effect = ccxt.NetworkError("API down")

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        for _ in range(2):
            with pytest.raises(OrderCancellationError):
                await service.cancel_order("order123", "BTC/USD")

        # Circuit is now open — should raise OrderCancellationError, not DataFetchError
        with pytest.raises(OrderCancellationError, match="Circuit breaker open"):
            await service.cancel_order("order123", "BTC/USD")

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_circuit_breaker_does_not_wrap_get_exchange_status(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.fetch_balance.side_effect = ccxt.NetworkError("API down")

        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        # Trip the breaker
        for _ in range(2):
            with pytest.raises(DataFetchError):
                await service.get_balance()

        assert service.circuit_breaker.state == CircuitState.OPEN

        # get_exchange_status should still work — it's not wrapped
        service.exchange.fetch_status = AsyncMock(
            return_value={"status": "ok", "updated": None, "eta": None, "url": None, "info": "ok"},
        )
        result = await service.get_exchange_status()
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    async def test_circuit_breaker_initialized_from_config(
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

        assert service.circuit_breaker.failure_threshold == 2
        assert service.circuit_breaker.recovery_timeout == 60.0
        assert service.circuit_breaker.half_open_max_calls == 1

    @pytest.mark.asyncio
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.parametrize(
        ("error", "raised"),
        [
            (ccxt.InsufficientFunds("balance too low"), OrderRejectedError),
            (ccxt.InvalidOrder("Invalid quantity"), OrderRejectedError),
            (ccxt.DuplicateOrderId("duplicate"), OrderOutcomeUnknownError),
        ],
    )
    async def test_refusals_and_duplicate_ids_never_open_the_breaker(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
        error,
        raised,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        mock_exchange_instance.create_order.side_effect = error
        service = LiveExchangeService(config_manager, is_paper_trading_activated=False)

        for _ in range(3):
            with pytest.raises(raised):
                await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0, client_order_id="g1")

        assert service.circuit_breaker.state == CircuitState.CLOSED


class TestFetchRecentOhlcv:
    @pytest.fixture
    def config_manager(self, valid_config):
        return config_manager_from(
            valid_config, {"exchange": {"name": "binance", "trading_mode": "live", "post_only": False}}
        )

    @pytest.fixture
    def mock_exchange_instance(self):
        return AsyncMock()

    @pytest.fixture
    def setup_env_vars(self, monkeypatch):
        monkeypatch.setenv("EXCHANGE_API_KEY", "test_api_key")
        monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test_secret_key")

    @pytest.fixture
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def live_service_fixture(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        return LiveExchangeService(config_manager, is_paper_trading_activated=False)

    @pytest.mark.asyncio
    async def test_returns_dataframe_with_ohlcv_columns(self, live_service_fixture):
        service = live_service_fixture
        service.exchange.fetch_ohlcv = AsyncMock(
            return_value=[
                [1700000000000, 100.0, 101.0, 99.0, 100.5, 12.0],
                [1700000060000, 100.5, 102.0, 100.0, 101.5, 8.0],
            ],
        )

        df = await service.fetch_recent_ohlcv("BTC/USDT", "1m", limit=2)

        assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
        assert len(df) == 2
        assert df["close"].iloc[-1] == 101.5

    @pytest.mark.asyncio
    async def test_wraps_exchange_error_in_data_fetch_error(self, live_service_fixture):
        service = live_service_fixture
        service.exchange.fetch_ohlcv = AsyncMock(side_effect=ccxt.BaseError("boom"))

        with pytest.raises(DataFetchError, match="Failed to fetch recent OHLCV for BTC/USDT"):
            await service.fetch_recent_ohlcv("BTC/USDT", "1m", limit=2)


class TestDeviationAnchorRelease:
    @pytest.fixture
    def config_manager(self, valid_config):
        return config_manager_from(
            valid_config, {"exchange": {"name": "binance", "trading_mode": "live", "post_only": False}}
        )

    @pytest.fixture
    def setup_env_vars(self, monkeypatch):
        monkeypatch.setenv("EXCHANGE_API_KEY", "test_api_key")
        monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test_secret_key")

    @pytest.fixture
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def exchange_service(self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars):
        mock_ccxtpro.binance.return_value = AsyncMock()
        mock_getattr.return_value = mock_ccxtpro.binance
        return LiveExchangeService(config_manager, is_paper_trading_activated=False)

    def test_reanchors_after_five_consecutive_rejections(self, exchange_service):
        exchange_service._last_known_price = 100.0
        for _ in range(4):
            assert exchange_service._validate_price(200.0, "SOL/USDT") is None
        assert exchange_service._validate_price(200.0, "SOL/USDT") == 200.0
        assert exchange_service._last_known_price == 200.0

    def test_accepted_price_resets_rejection_counter(self, exchange_service):
        exchange_service._last_known_price = 100.0
        for _ in range(3):
            exchange_service._validate_price(200.0, "SOL/USDT")
        exchange_service._validate_price(101.0, "SOL/USDT")  # accepted
        for _ in range(4):
            assert exchange_service._validate_price(200.0, "SOL/USDT") is None  # counter restarted

    def test_invalid_rejections_do_not_touch_counter(self, exchange_service):
        exchange_service._last_known_price = 100.0
        for _ in range(3):
            exchange_service._validate_price(200.0, "SOL/USDT")
        # None/non-numeric/non-finite/non-positive rejections must not advance the counter
        for bad_value in (None, "not-a-number", float("nan"), float("inf"), -5.0, 0.0):
            assert exchange_service._validate_price(bad_value, "SOL/USDT") is None
        # counter should still be at 3, so one more deviation rejection stays rejected...
        assert exchange_service._validate_price(200.0, "SOL/USDT") is None
        # ...and the 5th deviation rejection re-anchors
        assert exchange_service._validate_price(200.0, "SOL/USDT") == 200.0

    def test_accepted_price_records_timestamp(self, exchange_service):
        assert exchange_service.last_price_update_ts is None
        exchange_service._validate_price(100.0, "SOL/USDT")
        assert exchange_service.last_price_update_ts is not None


class TestWebSocketFailureHandling:
    @pytest.fixture
    def config_manager(self, valid_config):
        return config_manager_from(
            valid_config, {"exchange": {"name": "binance", "trading_mode": "live", "post_only": False}}
        )

    @pytest.fixture
    def setup_env_vars(self, monkeypatch):
        monkeypatch.setenv("EXCHANGE_API_KEY", "test_api_key")
        monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test_secret_key")

    @pytest.fixture
    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    def exchange_service(self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars):
        mock_ccxtpro.binance.return_value = AsyncMock()
        mock_getattr.return_value = mock_ccxtpro.binance
        return LiveExchangeService(config_manager, is_paper_trading_activated=False)

    @pytest.mark.asyncio
    @pytest.mark.timeout(5)
    async def test_generic_exceptions_respect_max_retries_and_raise(self, exchange_service):
        exchange_service.websocket_max_retries = 2
        exchange_service.websocket_retry_base_delay = 0
        exchange_service.exchange.watch_ticker = AsyncMock(side_effect=RuntimeError("session closed"))
        exchange_service.exchange.close = AsyncMock()

        with pytest.raises(DataFetchError, match="failed after"):
            await exchange_service.listen_to_ticker_updates("SOL/USDT", AsyncMock(), 0.0)

        assert exchange_service.connection_active is False
        exchange_service.exchange.close.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.timeout(5)
    async def test_network_error_exhaustion_raises(self, exchange_service):
        exchange_service.websocket_max_retries = 2
        exchange_service.websocket_retry_base_delay = 0
        exchange_service.exchange.watch_ticker = AsyncMock(side_effect=ccxt.NetworkError("down"))
        exchange_service.exchange.close = AsyncMock()

        with pytest.raises(DataFetchError):
            await exchange_service.listen_to_ticker_updates("SOL/USDT", AsyncMock(), 0.0)

        assert exchange_service.connection_active is False

    @pytest.mark.asyncio
    @pytest.mark.timeout(5)
    async def test_close_connection_exits_without_raising(self, exchange_service):
        exchange_service.exchange.watch_ticker = AsyncMock(return_value={"last": 100.0})
        exchange_service.exchange.close = AsyncMock()
        on_ticker_update = AsyncMock(side_effect=lambda _price: setattr(exchange_service, "connection_active", False))

        await exchange_service.listen_to_ticker_updates("SOL/USDT", on_ticker_update, 0.0)

        assert exchange_service.connection_active is False
        exchange_service.exchange.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_close_closes_the_exchange_once(self, exchange_service):
        exchange_service.exchange.close = AsyncMock()

        await exchange_service.close()
        await exchange_service.close()

        exchange_service.exchange.close.assert_awaited_once()
        assert exchange_service.connection_active is False

    @pytest.mark.asyncio
    async def test_close_logs_a_close_error_without_raising(self, exchange_service):
        exchange_service.exchange.close = AsyncMock(side_effect=Exception("Close error"))

        with patch.object(exchange_service.logger, "error") as mock_error:
            await exchange_service.close()

        mock_error.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(5)
    async def test_an_error_after_a_requested_stop_ends_the_loop_without_retrying(self, exchange_service):
        exchange_service.websocket_max_retries = 5
        exchange_service.websocket_retry_base_delay = 30

        async def watch_ticker(_pair):
            await exchange_service.close_connection()
            raise RuntimeError("instance was closed by the user")

        exchange_service.exchange.watch_ticker = watch_ticker

        await asyncio.wait_for(exchange_service.listen_to_ticker_updates("SOL/USDT", AsyncMock(), 0.0), timeout=1)

    @pytest.mark.asyncio
    @pytest.mark.timeout(5)
    async def test_a_feed_stopped_before_it_starts_never_listens(self, exchange_service):
        exchange_service.exchange.watch_ticker = AsyncMock()

        await exchange_service.close_connection()
        await asyncio.wait_for(exchange_service.listen_to_ticker_updates("SOL/USDT", AsyncMock(), 0.0), timeout=1)

        exchange_service.exchange.watch_ticker.assert_not_awaited()


class TestLiveExchangeServicePostOnly:
    """
    `exchange.post_only` makes grid limit orders maker-only: the exchange rejects them
    instead of letting them cross and be charged the taker fee.
    """

    @pytest.fixture
    def config_manager(self, valid_config):
        return config_manager_from(
            valid_config, {"exchange": {"name": "binance", "trading_mode": "live", "post_only": False}}
        )

    @pytest.fixture
    def mock_exchange_instance(self):
        exchange = AsyncMock()
        exchange.amount_to_precision = Mock(side_effect=lambda pair, amount: str(amount))
        exchange.price_to_precision = Mock(side_effect=lambda pair, price: str(price))
        return exchange

    @pytest.fixture
    def setup_env_vars(self, monkeypatch):
        monkeypatch.setenv("EXCHANGE_API_KEY", "test_api_key")
        monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test_secret_key")

    def _service(self, mock_getattr, mock_ccxtpro, config_manager, mock_exchange_instance):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        return LiveExchangeService(config_manager, is_paper_trading_activated=False)

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_limit_order_is_posted_maker_only_when_enabled(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        _with_exchange(config_manager, post_only=True)
        service = self._service(mock_getattr, mock_ccxtpro, config_manager, mock_exchange_instance)

        await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0)

        _, kwargs = mock_exchange_instance.create_order.call_args
        assert kwargs["params"]["postOnly"] is True

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_market_order_is_never_posted_maker_only(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        _with_exchange(config_manager, post_only=True)
        service = self._service(mock_getattr, mock_ccxtpro, config_manager, mock_exchange_instance)

        await service.place_order("BTC/USD", "market", "buy", 1, 50000.0)

        _, kwargs = mock_exchange_instance.create_order.call_args
        assert "postOnly" not in kwargs["params"]

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_limit_order_carries_no_post_only_flag_when_disabled(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        service = self._service(mock_getattr, mock_ccxtpro, config_manager, mock_exchange_instance)

        await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0)

        _, kwargs = mock_exchange_instance.create_order.call_args
        assert "postOnly" not in kwargs["params"]

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_a_maker_only_order_carries_its_client_order_id_too(
        self,
        mock_getattr,
        mock_ccxtpro,
        config_manager,
        setup_env_vars,
        mock_exchange_instance,
    ):
        _with_exchange(config_manager, post_only=True)
        service = self._service(mock_getattr, mock_ccxtpro, config_manager, mock_exchange_instance)

        await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0, client_order_id="g1")

        _, kwargs = mock_exchange_instance.create_order.call_args
        assert kwargs["params"] == {"postOnly": True, "clientOrderId": "g1"}


class TestLiveExchangeServicePostOnlyRejection:
    """
    A maker-only order the exchange refuses because it would cross is an expected outcome,
    not a fault: it must surface as its own type so callers can skip the level instead of
    reporting a failed placement.
    """

    @pytest.fixture
    def config_manager(self, valid_config):
        return config_manager_from(
            valid_config,
            {
                "exchange": {"name": "binance", "trading_mode": "live", "post_only": True},
                "execution": {"circuit_breaker_failure_threshold": 3},
            },
        )

    @pytest.fixture
    def mock_exchange_instance(self):
        exchange = AsyncMock()
        exchange.amount_to_precision = Mock(side_effect=lambda pair, amount: str(amount))
        exchange.price_to_precision = Mock(side_effect=lambda pair, price: str(price))
        return exchange

    @pytest.fixture
    def setup_env_vars(self, monkeypatch):
        monkeypatch.setenv("EXCHANGE_API_KEY", "test_api_key")
        monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test_secret_key")

    def _service(self, mock_getattr, mock_ccxtpro, config_manager, mock_exchange_instance):
        mock_getattr.return_value = mock_ccxtpro.binance
        mock_ccxtpro.binance.return_value = mock_exchange_instance
        return LiveExchangeService(config_manager, is_paper_trading_activated=False)

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_ccxt_post_only_error_becomes_post_only_rejected(
        self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars, mock_exchange_instance
    ):
        service = self._service(mock_getattr, mock_ccxtpro, config_manager, mock_exchange_instance)
        mock_exchange_instance.create_order.side_effect = ccxt.OrderImmediatelyFillable("would match")

        with pytest.raises(PostOnlyRejectedError):
            await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0)

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_generic_invalid_order_naming_post_only_is_recognised(
        self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars, mock_exchange_instance
    ):
        """Not every venue maps to OrderImmediatelyFillable; Kraken reports 'Post only order'."""
        service = self._service(mock_getattr, mock_ccxtpro, config_manager, mock_exchange_instance)
        mock_exchange_instance.create_order.side_effect = ccxt.InvalidOrder("EOrder:Post only order")

        with pytest.raises(PostOnlyRejectedError):
            await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0)

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_unrelated_invalid_order_is_an_exchange_refusal(
        self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars, mock_exchange_instance
    ):
        """A genuinely bad order must not be laundered into a benign maker-only rejection."""
        service = self._service(mock_getattr, mock_ccxtpro, config_manager, mock_exchange_instance)
        mock_exchange_instance.create_order.side_effect = ccxt.InvalidOrder("Invalid price precision")

        with pytest.raises(OrderRejectedError) as raised:
            await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0)
        assert not isinstance(raised.value, PostOnlyRejectedError)

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_post_only_wording_is_ignored_when_the_flag_is_off(
        self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars, mock_exchange_instance
    ):
        """With post_only off no maker-only order was ever sent, so this cannot be that rejection."""
        _with_exchange(config_manager, post_only=False)
        service = self._service(mock_getattr, mock_ccxtpro, config_manager, mock_exchange_instance)
        mock_exchange_instance.create_order.side_effect = ccxt.InvalidOrder("EOrder:Post only order")

        with pytest.raises(OrderRejectedError) as raised:
            await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0)
        assert not isinstance(raised.value, PostOnlyRejectedError)

    @patch("grid_trading_bot.core.services.live_exchange_service.ccxtpro")
    @patch("grid_trading_bot.core.services.live_exchange_service.getattr")
    @pytest.mark.asyncio
    async def test_repeated_rejections_never_open_the_circuit_breaker(
        self, mock_getattr, mock_ccxtpro, config_manager, setup_env_vars, mock_exchange_instance
    ):
        service = self._service(mock_getattr, mock_ccxtpro, config_manager, mock_exchange_instance)
        mock_exchange_instance.create_order.side_effect = ccxt.OrderImmediatelyFillable("would match")

        for _ in range(10):
            with pytest.raises(PostOnlyRejectedError):
                await service.place_order("BTC/USD", "limit", "buy", 1, 50000.0)

        assert service.circuit_breaker.state == CircuitState.CLOSED

    def test_post_only_rejection_is_not_a_data_fetch_error(self):
        """The execution strategy wraps DataFetchError; this must slip past that wrapping."""
        assert not issubclass(PostOnlyRejectedError, DataFetchError)
