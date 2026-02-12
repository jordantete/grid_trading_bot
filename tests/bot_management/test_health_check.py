import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from grid_trading_bot.core.bot_management.event_bus import EventBus
from grid_trading_bot.core.bot_management.grid_trading_bot import GridTradingBot
from grid_trading_bot.core.bot_management.health_check import HealthCheck, ResourceMetrics
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler


class TestHealthCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Mock(spec=GridTradingBot)
        self.notification_handler = Mock(spec=NotificationHandler)
        self.event_bus = Mock(spec=EventBus)

        # Mock psutil.Process for initialization
        with patch("psutil.Process") as mock_process:
            process_instance = MagicMock()
            mock_process.return_value = process_instance
            process_instance.cpu_percent = Mock(return_value=0.0)

            self.health_check = HealthCheck(
                bot=self.bot,
                notification_handler=self.notification_handler,
                event_bus=self.event_bus,
                check_interval=1,  # Set a low interval for testing
                metrics_history_size=5,  # Small size for testing
            )

    @patch("psutil.Process")
    def test_initialization(self, mock_process):
        """Test that the HealthCheck is properly initialized with metrics history"""
        health_check = HealthCheck(
            bot=self.bot,
            notification_handler=self.notification_handler,
            event_bus=self.event_bus,
        )

        assert health_check.metrics_history_size == 60
        assert len(health_check._metrics_history) == 0
        mock_process.return_value.cpu_percent.assert_called_once()

    @patch("psutil.cpu_percent", return_value=95)
    @patch("psutil.virtual_memory")
    @patch("psutil.disk_usage")
    @patch("psutil.Process")
    def test_check_resource_usage(self, mock_process, mock_disk_usage, mock_virtual_memory, mock_cpu_percent):
        mock_virtual_memory.return_value.percent = 85
        mock_disk_usage.return_value.percent = 10

        process_instance = MagicMock()
        mock_process.return_value = process_instance
        process_instance.memory_info.return_value.rss = 100000000
        process_instance.open_files.return_value = []
        process_instance.num_threads.return_value = 4

        usage = self.health_check._check_resource_usage()

        assert usage["cpu"] == 95
        assert usage["memory"] == 85
        assert usage["disk"] == 10

    async def test_resource_metrics_collection(self):
        """Test that resource metrics are properly collected and stored"""
        with (
            patch("psutil.Process") as mock_process,
            patch("psutil.cpu_percent") as mock_cpu,
            patch("psutil.virtual_memory") as mock_memory,
            patch("psutil.disk_usage") as mock_disk,
        ):
            # Setup mock returns
            mock_cpu.return_value = 50.0
            mock_memory.return_value.percent = 60.0
            mock_memory.return_value.total = 16000000000  # 16GB
            mock_memory.return_value.available = 8000000000  # 8GB
            mock_disk.return_value.percent = 70.0

            # Create a mock process instance
            process_instance = MagicMock()
            mock_process.return_value = process_instance
            process_instance.cpu_percent.return_value = 25.0
            process_instance.open_files.return_value = ["file1", "file2"]

            metrics = self.health_check._check_resource_usage()

            assert metrics["cpu"] == 50.0
            assert metrics["memory"] == 60.0
            assert metrics["disk"] == 70.0
            assert metrics["memory_available_mb"] == 8000000000 / (1024 * 1024)

    def test_metrics_history_management(self):
        """Test that metrics history is properly managed"""
        # Create some test metrics
        for i in range(10):  # More than metrics_history_size
            metrics = ResourceMetrics(
                timestamp=datetime.now(tz=UTC) + timedelta(minutes=i),
                cpu_percent=50.0 + i,
                memory_percent=60.0 + i,
                disk_percent=70.0,
                bot_cpu_percent=25.0,
                bot_memory_mb=100.0,
                open_files=2,
                thread_count=4,
            )
            self.health_check._metrics_history.append(metrics)
            if len(self.health_check._metrics_history) > self.health_check.metrics_history_size:
                self.health_check._metrics_history.pop(0)

        assert len(self.health_check._metrics_history) == 5  # Should match metrics_history_size
        assert self.health_check._metrics_history[-1].cpu_percent > self.health_check._metrics_history[0].cpu_percent

    def test_resource_trends_calculation(self):
        """Test that resource usage trends are correctly calculated"""
        now = datetime.now(tz=UTC)
        one_hour_ago = now - timedelta(hours=1)

        self.health_check._metrics_history = [
            ResourceMetrics(
                timestamp=one_hour_ago,
                cpu_percent=50.0,
                memory_percent=60.0,
                disk_percent=70.0,
                bot_cpu_percent=25.0,
                bot_memory_mb=100.0,
                open_files=2,
                thread_count=4,
            ),
            ResourceMetrics(
                timestamp=now,
                cpu_percent=60.0,  # 10% increase over 1 hour
                memory_percent=70.0,  # 10% increase
                disk_percent=70.0,
                bot_cpu_percent=35.0,  # 10% increase
                bot_memory_mb=120.0,  # 20MB increase
                open_files=2,
                thread_count=4,
            ),
        ]

        trends = self.health_check.get_resource_trends()

        assert abs(trends["cpu_trend"] - 10.0) < 0.01
        assert abs(trends["memory_trend"] - 10.0) < 0.01
        assert abs(trends["bot_cpu_trend"] - 10.0) < 0.01
        assert abs(trends["bot_memory_trend"] - 20.0) < 0.01

    async def test_resource_alerts_with_trends(self):
        """Test that resource alerts include trend information"""
        # Setup resource history
        self.health_check._metrics_history = [
            ResourceMetrics(
                timestamp=datetime.now(tz=UTC) - timedelta(hours=1),
                cpu_percent=80.0,
                memory_percent=70.0,
                disk_percent=70.0,
                bot_cpu_percent=25.0,
                bot_memory_mb=100.0,
                open_files=2,
                thread_count=4,
            ),
            ResourceMetrics(
                timestamp=datetime.now(tz=UTC),
                cpu_percent=95.0,
                memory_percent=85.0,
                disk_percent=70.0,
                bot_cpu_percent=35.0,
                bot_memory_mb=150.0,
                open_files=2,
                thread_count=4,
            ),
        ]

        usage = {
            "cpu": 95.0,
            "memory": 85.0,
            "disk": 70.0,
            "bot_cpu": 35.0,
            "bot_memory_mb": 150.0,
        }

        await self.health_check._check_and_alert_resource_usage(usage)

        # Verify that notifications include trend information
        self.notification_handler.async_send_notification.assert_awaited_once()
        call_args = self.notification_handler.async_send_notification.await_args[1]
        assert "Trend: increasing" in call_args["alert_details"]
        assert "CPU usage is high: 95.0%" in call_args["alert_details"]
        assert "MEMORY usage is high: 85.0%" in call_args["alert_details"]

    async def test_start_and_stop(self):
        self.health_check._perform_checks = AsyncMock()
        self.health_check._is_running = False  # Ensure it starts

        start_task = asyncio.create_task(self.health_check.start())
        await asyncio.sleep(0.1)  # Give it time to start

        assert self.health_check._is_running
        self.health_check._perform_checks.assert_called_once()

        self.health_check._is_running = False
        await asyncio.sleep(0.1)  # Allow for loop termination
        start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start_task

        assert not self.health_check._is_running

    async def test_stop_event(self):
        self.health_check._is_running = True
        reason = "User initiated stop"

        self.health_check._handle_stop(reason)

        assert not self.health_check._is_running

    async def test_start_event(self):
        self.health_check._is_running = False
        self.health_check.start = AsyncMock()

        await self.health_check._handle_start("User initiated start")

        self.health_check.start.assert_awaited_once()

    async def test_perform_checks_success(self):
        self.bot.get_bot_health_status = AsyncMock(return_value={"strategy": True, "exchange_status": "ok"})
        self.health_check._check_resource_usage = Mock(return_value={"cpu": 10, "memory": 10, "disk": 10})

        self.health_check._check_and_alert_bot_health = AsyncMock()
        self.health_check._check_and_alert_resource_usage = AsyncMock()

        await self.health_check._perform_checks()

        self.health_check._check_and_alert_bot_health.assert_awaited_with({"strategy": True, "exchange_status": "ok"})
        self.health_check._check_and_alert_resource_usage.assert_awaited_with({"cpu": 10, "memory": 10, "disk": 10})

    async def test_check_and_alert_bot_health_with_alerts(self):
        health_status = {"strategy": False, "exchange_status": "maintenance"}

        await self.health_check._check_and_alert_bot_health(health_status)

        self.notification_handler.async_send_notification.assert_awaited_once_with(
            NotificationType.HEALTH_CHECK_ALERT,
            alert_details="Trading strategy has encountered issues. | Exchange status is not ok: maintenance",
        )

    async def test_check_and_alert_bot_health_no_alerts(self):
        health_status = {"strategy": True, "exchange_status": "ok"}

        await self.health_check._check_and_alert_bot_health(health_status)

        self.notification_handler.async_send_notification.assert_not_awaited()

    async def test_check_and_alert_resource_usage_with_alerts(self):
        usage = {"cpu": 95, "memory": 85, "disk": 10}

        # Initialize empty metrics history to get "stable" trend
        self.health_check._metrics_history = []

        await self.health_check._check_and_alert_resource_usage(usage)

        expected_message = (
            "CPU usage is high: 95.0% (Threshold: 90%, Trend: stable) | "
            "MEMORY usage is high: 85.0% (Threshold: 80%, Trend: stable)"
        )
        self.notification_handler.async_send_notification.assert_awaited_once_with(
            NotificationType.HEALTH_CHECK_ALERT,
            alert_details=expected_message,
        )

    async def test_check_and_alert_resource_usage_no_alerts(self):
        usage = {"cpu": 10, "memory": 10, "disk": 10}

        await self.health_check._check_and_alert_resource_usage(usage)

        self.notification_handler.async_send_notification.assert_not_awaited()

    async def test_start_already_running(self):
        self.health_check._is_running = True
        self.health_check.logger.warning = Mock()

        await self.health_check.start()

        self.health_check.logger.warning.assert_called_once_with("HealthCheck is already running.")

    def test_handle_stop_when_not_running(self):
        self.health_check._is_running = False
        self.health_check.logger.warning = Mock()

        self.health_check._handle_stop("Already stopped")

        self.health_check.logger.warning.assert_called_once_with("HealthCheck is not running.")

    async def asyncTearDown(self):
        """Clean up any pending tasks after each test"""
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
