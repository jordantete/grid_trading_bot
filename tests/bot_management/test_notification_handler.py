from unittest.mock import Mock, patch

import pytest

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus, OrderType


class TestNotificationHandler:
    @pytest.fixture
    def notification_handler_enabled(self):
        urls = ["json://localhost:8080/path"]
        handler = NotificationHandler(
            urls=urls,
            trading_mode=TradingMode.LIVE,
        )
        return handler

    @pytest.fixture
    def notification_handler_disabled(self):
        return NotificationHandler(
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
    def test_notification_handler_enabled_initialization(self, mock_apprise):
        handler = NotificationHandler(
            urls=["mock://example.com"],
            trading_mode=TradingMode.LIVE,
        )
        assert handler.enabled is True
        mock_apprise.return_value.add.assert_called_once_with("mock://example.com")

    @patch("apprise.Apprise")
    def test_notification_handler_disabled_initialization(self, mock_apprise):
        handler = NotificationHandler(
            urls=None,
            trading_mode=TradingMode.BACKTEST,
        )
        assert handler.enabled is False
        mock_apprise.assert_not_called()

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

        with patch.object(handler, "send_notification") as mock_send:
            await handler.async_send_notification(
                NotificationType.ORDER_FILLED,
                order_details="test",
            )

            mock_send.assert_called_once_with(
                NotificationType.ORDER_FILLED,
                order_details="test",
            )

    @pytest.mark.asyncio
    async def test_a_disabled_handler_never_reaches_its_executor(self, notification_handler_disabled):
        with patch.object(notification_handler_disabled, "send_notification") as send:
            await notification_handler_disabled.async_send_notification(
                NotificationType.ORDER_FILLED, order_details="x"
            )

        send.assert_not_called()

    def test_send_notification_disabled(self, notification_handler_disabled):
        handler = notification_handler_disabled
        with patch("apprise.Apprise.notify") as mock_notify:
            handler.send_notification(NotificationType.ORDER_FILLED, order_details="test")
            mock_notify.assert_not_called()

    @pytest.mark.parametrize("urls", [None, []])
    @pytest.mark.parametrize("mode", [TradingMode.LIVE, TradingMode.PAPER_TRADING])
    def test_warns_when_live_notifications_are_disabled(self, urls, mode, caplog):
        NotificationHandler(urls=urls, trading_mode=mode)

        assert "Notifications are disabled: APPRISE_NOTIFICATION_URLS is empty." in caplog.text

    def test_no_warning_in_backtest(self, caplog):
        NotificationHandler(urls=None, trading_mode=TradingMode.BACKTEST)

        assert "Notifications are disabled" not in caplog.text

    def test_logs_an_undelivered_notification(self, notification_handler_enabled, caplog):
        notification_handler_enabled.apprise_instance = Mock()
        notification_handler_enabled.apprise_instance.notify.return_value = False

        notification_handler_enabled.send_notification(NotificationType.ERROR_OCCURRED, error_details="x")

        assert "Notification not delivered: Error Occurred" in caplog.text


async def test_a_notification_logs_into_the_file_of_its_bot(tmp_path, monkeypatch):
    import logging

    from grid_trading_bot.config.trading_mode import TradingMode
    from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
    from grid_trading_bot.utils.logging_config import add_bot_log_file, configure_console, current_bot

    monkeypatch.chdir(tmp_path)
    current_bot.set("bot_a")
    configure_console(logging.INFO)
    add_bot_log_file("bot_a", logging.INFO)
    handler = NotificationHandler(["json://localhost"], TradingMode.LIVE)

    def send(content, **kwargs):
        logging.getLogger("NotificationHandler").error("Notification not delivered: probe")

    monkeypatch.setattr(handler, "send_notification", send)

    await handler.async_send_notification("probe")

    lines = (tmp_path / "logs" / "bot_a.log").read_text().splitlines()
    assert any("NotificationHandler - ERROR - Notification not delivered: probe" in line for line in lines)
