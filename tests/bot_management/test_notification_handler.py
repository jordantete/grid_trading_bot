from unittest.mock import Mock, patch

import pytest

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus, OrderType


class TestNotificationHandler:
    @pytest.fixture
    def event_bus(self):
        return Mock(spec=EventBus)

    @pytest.fixture
    def notification_handler_enabled(self, event_bus):
        urls = ["json://localhost:8080/path"]
        handler = NotificationHandler(
            event_bus=event_bus,
            urls=urls,
            trading_mode=TradingMode.LIVE,
        )
        return handler

    @pytest.fixture
    def notification_handler_disabled(self, event_bus):
        return NotificationHandler(
            event_bus=event_bus,
            urls=None,
            trading_mode=TradingMode.BACKTEST,
        )

    @pytest.fixture
    def mock_order(self):
        return Order(
            identifier="test-123",
            status=OrderStatus.CLOSED,
            order_type=OrderType.LIMIT,
            side=OrderSide.BUY,
            price=1000.0,
            average=1000.0,
            amount=1.0,
            filled=1.0,
            remaining=0.0,
            timestamp=1234567890000,
            datetime="2024-01-01T00:00:00Z",
            last_trade_timestamp="2024-01-01T00:00:00Z",
            symbol="BTC/USDT",
            time_in_force="GTC",
        )

    @patch("apprise.Apprise")
    def test_notification_handler_enabled_initialization(self, mock_apprise, event_bus):
        handler = NotificationHandler(
            event_bus=event_bus,
            urls=["mock://example.com"],
            trading_mode=TradingMode.LIVE,
        )
        assert handler.enabled is True
        mock_apprise.return_value.add.assert_called_once_with("mock://example.com")
        event_bus.subscribe.assert_called_once_with(Events.ORDER_FILLED, handler._send_notification_on_order_filled)

    @patch("apprise.Apprise")
    def test_notification_handler_disabled_initialization(self, mock_apprise, event_bus):
        handler = NotificationHandler(
            event_bus=event_bus,
            urls=None,
            trading_mode=TradingMode.BACKTEST,
        )
        assert handler.enabled is False
        mock_apprise.assert_not_called()
        event_bus.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_notification_with_predefined_content(self, notification_handler_enabled, mock_order):
        handler = notification_handler_enabled
        with patch.object(handler.apprise_instance, "notify") as mock_notify:
            handler.send_notification(
                NotificationType.ORDER_FILLED,
                order_details=str(mock_order),
            )

            mock_notify.assert_called_once_with(
                title="Order Filled",
                body=f"Order has been filled successfully:\n{mock_order!s}",
            )

    @pytest.mark.asyncio
    async def test_send_notification_with_missing_placeholder(self, notification_handler_enabled):
        handler = notification_handler_enabled
        with (
            patch.object(handler.apprise_instance, "notify") as mock_notify,
            patch("logging.Logger.warning") as mock_warning,
        ):
            handler.send_notification(NotificationType.ORDER_FILLED)

            mock_warning.assert_called_once_with(
                "Missing placeholders for notification: {'order_details'}. Defaulting to 'N/A' for missing values.",
            )
            mock_notify.assert_called_once_with(
                title="Order Filled",
                body="Order has been filled successfully:\nN/A",
            )

    @pytest.mark.asyncio
    async def test_send_notification_with_order_failed(self, notification_handler_enabled):
        handler = notification_handler_enabled
        error_details = "Insufficient funds"

        with patch.object(handler.apprise_instance, "notify") as mock_notify:
            handler.send_notification(
                NotificationType.ORDER_FAILED,
                error_details=error_details,
            )

            mock_notify.assert_called_once_with(
                title="Order Placement Failed",
                body=f"Failed to place order:\n{error_details}",
            )

    @pytest.mark.asyncio
    async def test_async_send_notification_success(self, notification_handler_enabled):
        handler = notification_handler_enabled

        # Mock both the executor and send_notification
        with (
            patch.object(handler, "_executor", create=True) as mock_executor,
            patch.object(handler, "send_notification") as mock_send,
        ):
            # Configure the mock executor to run the function directly
            mock_executor.submit = lambda f, *args, **kwargs: f(*args, **kwargs)

            await handler.async_send_notification(
                NotificationType.ORDER_FILLED,
                order_details="test",
            )

            mock_send.assert_called_once_with(
                NotificationType.ORDER_FILLED,
                order_details="test",
            )

    @pytest.mark.asyncio
    async def test_event_subscription_and_notification_on_order_filled(
        self,
        notification_handler_enabled,
        mock_order,
    ):
        handler = notification_handler_enabled
        with patch.object(handler, "async_send_notification") as mock_async_send:
            await handler._send_notification_on_order_filled(mock_order)

            mock_async_send.assert_called_once_with(NotificationType.ORDER_FILLED, order_details=str(mock_order))

    def test_send_notification_disabled(self, notification_handler_disabled):
        handler = notification_handler_disabled
        with patch("apprise.Apprise.notify") as mock_notify:
            handler.send_notification(NotificationType.ORDER_FILLED, order_details="test")
            mock_notify.assert_not_called()
