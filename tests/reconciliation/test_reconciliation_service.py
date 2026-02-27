import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest

from grid_trading_bot.core.bot_management.event_bus import EventBus
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.reconciliation.reconciliation_service import ReconciliationService
from grid_trading_bot.core.services.exceptions import DataFetchError


@pytest.fixture
def setup_reconciliation_service():
    order_book = Mock()
    balance_tracker = Mock()
    balance_tracker.balance = 5000.0
    balance_tracker.crypto_balance = 1.5
    exchange_service = Mock()
    exchange_service.fetch_open_orders = AsyncMock(return_value=[])
    exchange_service.get_balance = AsyncMock(
        return_value={"free": {"USDT": 5000.0, "BTC": 1.5}, "used": {}, "total": {}}
    )
    notification_handler = Mock()
    notification_handler.async_send_notification = AsyncMock()
    event_bus = Mock(spec=EventBus)

    service = ReconciliationService(
        order_book=order_book,
        balance_tracker=balance_tracker,
        exchange_service=exchange_service,
        notification_handler=notification_handler,
        event_bus=event_bus,
        trading_pair="BTC/USDT",
        base_currency="BTC",
        quote_currency="USDT",
        reconciliation_interval=1.0,
        balance_tolerance=0.01,
        alert_cooldown=900,
    )
    return service, order_book, balance_tracker, exchange_service, notification_handler, event_bus


class TestOrderReconciliation:
    @pytest.mark.asyncio
    async def test_no_mismatches_logs_ok(self, setup_reconciliation_service):
        service, order_book, _, exchange_service, notification_handler, _ = setup_reconciliation_service

        mock_order = Mock(identifier="order_1")
        order_book.get_open_orders.return_value = [mock_order]
        exchange_service.fetch_open_orders = AsyncMock(
            return_value=[{"id": "order_1", "side": "buy", "price": 50000, "amount": 0.1}]
        )

        with patch.object(service.logger, "info") as mock_info:
            await service._reconcile_orders()
            mock_info.assert_any_call("Order reconciliation OK: 1 local orders in sync with exchange.")

        notification_handler.async_send_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ghost_order_detected(self, setup_reconciliation_service):
        service, order_book, _, exchange_service, notification_handler, _ = setup_reconciliation_service

        mock_order = Mock(identifier="order_1")
        order_book.get_open_orders.return_value = [mock_order]
        exchange_service.fetch_open_orders = AsyncMock(return_value=[])

        with patch.object(service.logger, "warning") as mock_warning:
            await service._reconcile_orders()
            mock_warning.assert_any_call("GHOST ORDER: order_1 exists locally but NOT on exchange")

        notification_handler.async_send_notification.assert_awaited_once_with(
            NotificationType.RECONCILIATION_ORDER_MISMATCH,
            alert_details="GHOST ORDER: order_1 exists locally but NOT on exchange",
        )

    @pytest.mark.asyncio
    async def test_orphan_order_detected(self, setup_reconciliation_service):
        service, order_book, _, exchange_service, notification_handler, _ = setup_reconciliation_service

        order_book.get_open_orders.return_value = []
        exchange_service.fetch_open_orders = AsyncMock(
            return_value=[{"id": "orphan_1", "side": "sell", "price": 60000, "amount": 0.5}]
        )

        with patch.object(service.logger, "warning") as mock_warning:
            await service._reconcile_orders()
            mock_warning.assert_any_call(
                "ORPHAN ORDER: orphan_1 on exchange but NOT local (side=sell, price=60000, amount=0.5)"
            )

        notification_handler.async_send_notification.assert_awaited_once()
        call_args = notification_handler.async_send_notification.call_args
        assert call_args[0][0] == NotificationType.RECONCILIATION_ORDER_MISMATCH
        assert "ORPHAN ORDER" in call_args[1]["alert_details"]

    @pytest.mark.asyncio
    async def test_ghost_and_orphan_combined(self, setup_reconciliation_service):
        service, order_book, _, exchange_service, notification_handler, _ = setup_reconciliation_service

        mock_local = Mock(identifier="local_only")
        order_book.get_open_orders.return_value = [mock_local]
        exchange_service.fetch_open_orders = AsyncMock(
            return_value=[{"id": "remote_only", "side": "buy", "price": 50000, "amount": 0.2}]
        )

        await service._reconcile_orders()

        notification_handler.async_send_notification.assert_awaited_once()
        call_args = notification_handler.async_send_notification.call_args
        alert_details = call_args[1]["alert_details"]
        assert "GHOST ORDER" in alert_details
        assert "ORPHAN ORDER" in alert_details

    @pytest.mark.asyncio
    async def test_exchange_error_skips_order_reconciliation(self, setup_reconciliation_service):
        service, _, _, exchange_service, notification_handler, _ = setup_reconciliation_service

        exchange_service.fetch_open_orders = AsyncMock(side_effect=DataFetchError("Connection failed"))

        with patch.object(service.logger, "error") as mock_error:
            await service._reconcile_orders()
            mock_error.assert_called_once_with("Failed to fetch open orders from exchange: Connection failed")

        notification_handler.async_send_notification.assert_not_awaited()


class TestBalanceReconciliation:
    @pytest.mark.asyncio
    async def test_no_drift_logs_ok(self, setup_reconciliation_service):
        service, _, balance_tracker, exchange_service, notification_handler, _ = setup_reconciliation_service

        balance_tracker.balance = 5000.0
        balance_tracker.crypto_balance = 1.5
        exchange_service.get_balance = AsyncMock(return_value={"free": {"USDT": 5000.0, "BTC": 1.5}})

        with patch.object(service.logger, "info") as mock_info:
            await service._reconcile_balances()
            mock_info.assert_any_call("Balance reconciliation OK: local matches exchange within tolerance.")

        notification_handler.async_send_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fiat_drift_detected(self, setup_reconciliation_service):
        service, _, balance_tracker, exchange_service, notification_handler, _ = setup_reconciliation_service

        balance_tracker.balance = 5000.0
        balance_tracker.crypto_balance = 1.5
        exchange_service.get_balance = AsyncMock(return_value={"free": {"USDT": 4950.0, "BTC": 1.5}})

        with patch.object(service.logger, "warning") as mock_warning:
            await service._reconcile_balances()
            assert any("Fiat drift" in str(c) for c in mock_warning.call_args_list)

        notification_handler.async_send_notification.assert_awaited_once()
        call_args = notification_handler.async_send_notification.call_args
        assert call_args[0][0] == NotificationType.RECONCILIATION_BALANCE_DRIFT
        assert "Fiat drift" in call_args[1]["alert_details"]

    @pytest.mark.asyncio
    async def test_crypto_drift_detected(self, setup_reconciliation_service):
        service, _, balance_tracker, exchange_service, notification_handler, _ = setup_reconciliation_service

        balance_tracker.balance = 5000.0
        balance_tracker.crypto_balance = 1.5
        exchange_service.get_balance = AsyncMock(return_value={"free": {"USDT": 5000.0, "BTC": 1.6}})

        with patch.object(service.logger, "warning") as mock_warning:
            await service._reconcile_balances()
            assert any("Crypto drift" in str(c) for c in mock_warning.call_args_list)

        notification_handler.async_send_notification.assert_awaited_once()
        call_args = notification_handler.async_send_notification.call_args
        assert "Crypto drift" in call_args[1]["alert_details"]

    @pytest.mark.asyncio
    async def test_drift_within_tolerance_no_alert(self, setup_reconciliation_service):
        service, _, balance_tracker, exchange_service, notification_handler, _ = setup_reconciliation_service

        balance_tracker.balance = 5000.0
        balance_tracker.crypto_balance = 1.5
        exchange_service.get_balance = AsyncMock(return_value={"free": {"USDT": 5000.005, "BTC": 1.5003}})

        with patch.object(service.logger, "info") as mock_info:
            await service._reconcile_balances()
            mock_info.assert_any_call("Balance reconciliation OK: local matches exchange within tolerance.")

        notification_handler.async_send_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_both_fiat_and_crypto_drift(self, setup_reconciliation_service):
        service, _, balance_tracker, exchange_service, notification_handler, _ = setup_reconciliation_service

        balance_tracker.balance = 5000.0
        balance_tracker.crypto_balance = 1.5
        exchange_service.get_balance = AsyncMock(return_value={"free": {"USDT": 4800.0, "BTC": 1.8}})

        await service._reconcile_balances()

        notification_handler.async_send_notification.assert_awaited_once()
        call_args = notification_handler.async_send_notification.call_args
        alert_details = call_args[1]["alert_details"]
        assert "Fiat drift" in alert_details
        assert "Crypto drift" in alert_details

    @pytest.mark.asyncio
    async def test_exchange_error_skips_balance_reconciliation(self, setup_reconciliation_service):
        service, _, _, exchange_service, notification_handler, _ = setup_reconciliation_service

        exchange_service.get_balance = AsyncMock(side_effect=DataFetchError("Timeout"))

        with patch.object(service.logger, "error") as mock_error:
            await service._reconcile_balances()
            mock_error.assert_called_once_with("Failed to fetch balance from exchange: Timeout")

        notification_handler.async_send_notification.assert_not_awaited()


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_monitoring_task(self, setup_reconciliation_service):
        service, *_ = setup_reconciliation_service

        service.start()
        assert service._monitoring_task is not None
        assert not service._monitoring_task.done()

        await service.stop()

    @pytest.mark.asyncio
    async def test_start_warns_if_already_running(self, setup_reconciliation_service):
        service, *_ = setup_reconciliation_service

        service.start()
        with patch.object(service.logger, "warning") as mock_warning:
            service.start()
            mock_warning.assert_called_once_with("ReconciliationService is already running.")

        await service.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_monitoring_task(self, setup_reconciliation_service):
        service, *_ = setup_reconciliation_service

        service.start()
        assert service._monitoring_task is not None

        await service.stop()
        assert service._monitoring_task is None

    @pytest.mark.asyncio
    async def test_cleanup_unsubscribes_events(self, setup_reconciliation_service):
        service, _, _, _, _, event_bus = setup_reconciliation_service

        service.cleanup()

        event_bus.unsubscribe.assert_any_call(
            "stop_bot",
            service._handle_stop,
        )
        event_bus.unsubscribe.assert_any_call(
            "start_bot",
            service._handle_start,
        )

    @pytest.mark.asyncio
    async def test_handle_stop_cancels_task(self, setup_reconciliation_service):
        service, *_ = setup_reconciliation_service

        service.start()
        assert service._monitoring_task is not None

        service._handle_stop("User requested stop")
        await asyncio.sleep(0.1)
        assert service._monitoring_task.cancelled() or service._monitoring_task.done()

        # Cleanup
        service._monitoring_task = None


class TestAlertCooldown:
    @pytest.mark.asyncio
    async def test_alert_sent_first_time(self, setup_reconciliation_service):
        service, *_ = setup_reconciliation_service

        assert service._should_send_alert("test_key") is True

    @pytest.mark.asyncio
    async def test_alert_suppressed_within_cooldown(self, setup_reconciliation_service):
        service, *_ = setup_reconciliation_service

        assert service._should_send_alert("test_key") is True
        assert service._should_send_alert("test_key") is False

    @pytest.mark.asyncio
    async def test_alert_resent_after_cooldown(self, setup_reconciliation_service):
        service, *_ = setup_reconciliation_service

        assert service._should_send_alert("test_key") is True

        # Simulate time passing beyond cooldown
        service._last_alert_times["test_key"] = datetime.now(tz=UTC) - timedelta(seconds=901)

        assert service._should_send_alert("test_key") is True

    @pytest.mark.asyncio
    async def test_purge_stale_alerts(self, setup_reconciliation_service):
        service, *_ = setup_reconciliation_service

        service._last_alert_times["old_key"] = datetime.now(tz=UTC) - timedelta(seconds=2000)
        service._last_alert_times["recent_key"] = datetime.now(tz=UTC)

        service._purge_stale_alerts()

        assert "old_key" not in service._last_alert_times
        assert "recent_key" in service._last_alert_times

    @pytest.mark.asyncio
    async def test_duplicate_order_alert_suppressed(self, setup_reconciliation_service):
        service, order_book, _, exchange_service, notification_handler, _ = setup_reconciliation_service

        mock_order = Mock(identifier="order_1")
        order_book.get_open_orders.return_value = [mock_order]
        exchange_service.fetch_open_orders = AsyncMock(return_value=[])

        # First call: alert sent
        await service._reconcile_orders()
        assert notification_handler.async_send_notification.await_count == 1

        # Second call: alert suppressed by cooldown
        await service._reconcile_orders()
        assert notification_handler.async_send_notification.await_count == 1


class TestRunReconciliation:
    @pytest.mark.asyncio
    async def test_run_reconciliation_calls_both(self, setup_reconciliation_service):
        service, *_ = setup_reconciliation_service

        service._reconcile_orders = AsyncMock()
        service._reconcile_balances = AsyncMock()

        await service._run_reconciliation()

        service._reconcile_orders.assert_awaited_once()
        service._reconcile_balances.assert_awaited_once()
