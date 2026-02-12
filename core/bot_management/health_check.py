import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import logging

import psutil

from core.bot_management.event_bus import EventBus, Events
from core.bot_management.grid_trading_bot import GridTradingBot
from core.bot_management.notification.notification_content import NotificationType
from core.bot_management.notification.notification_handler import NotificationHandler
from utils.constants import RESOURCE_THRESHOLDS


@dataclass
class ResourceMetrics:
    timestamp: datetime
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    bot_cpu_percent: float
    bot_memory_mb: float
    open_files: int
    thread_count: int


class HealthCheck:
    """
    Periodically checks the bot's health and system resource usage and sends alerts if thresholds are exceeded.
    """

    def __init__(
        self,
        bot: GridTradingBot,
        notification_handler: NotificationHandler,
        event_bus: EventBus,
        check_interval: int = 60,
        metrics_history_size: int = 60,  # Keep 1 hour of metrics at 1-minute intervals
    ):
        """
        Initializes the HealthCheck.

        Args:
            bot: The GridTradingBot instance to monitor.
            notification_handler: The NotificationHandler for sending alerts.
            event_bus: The EventBus instance for listening to bot lifecycle events.
            check_interval: Time interval (in seconds) between health checks.
            metrics_history_size: Number of metrics to keep in the history.
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.bot = bot
        self.notification_handler = notification_handler
        self.event_bus = event_bus
        self.check_interval = check_interval
        self._is_running = False
        self._stop_event = asyncio.Event()
        self.process = psutil.Process()
        self._metrics_history: list[ResourceMetrics] = []
        self.metrics_history_size = metrics_history_size
        self.process.cpu_percent()  # First call to initialize CPU monitoring
        self.event_bus.subscribe(Events.STOP_BOT, self._handle_stop)
        self.event_bus.subscribe(Events.START_BOT, self._handle_start)

    async def start(self):
        """
        Starts the health check monitoring loop.
        """
        if self._is_running:
            self.logger.warning("HealthCheck is already running.")
            return

        self._is_running = True
        self._stop_event.clear()
        self.logger.info("HealthCheck started.")

        try:
            while self._is_running:
                await self._perform_checks()
                stop_task = asyncio.create_task(self._stop_event.wait())
                done, _ = await asyncio.wait([stop_task], timeout=self.check_interval)

                if stop_task in done:
                    # Stop event was triggered; exit loop
                    break

        except asyncio.CancelledError:
            self.logger.info("HealthCheck task cancelled.")

        except Exception as e:
            self.logger.error(f"Unexpected error in HealthCheck: {e}")
            await self.notification_handler.async_send_notification(
                NotificationType.ERROR_OCCURRED,
                error_details=f"Health check encountered an error: {e}",
            )

    async def _perform_checks(self):
        """
        Performs bot health and resource usage checks.
        """
        self.logger.info("Starting health checks for bot and system resources.")

        bot_health = await self.bot.get_bot_health_status()
        self.logger.info(f"Fetched bot health status: {bot_health}")
        await self._check_and_alert_bot_health(bot_health)

        resource_usage = self._check_resource_usage()
        self.logger.info(f"System resource usage: {resource_usage}")
        await self._check_and_alert_resource_usage(resource_usage)

    async def _check_and_alert_bot_health(self, health_status: dict):
        """
        Checks the bot's health status and sends alerts if necessary.

        Args:
            health_status: A dictionary containing the bot's health status.
        """
        alerts = []

        if not health_status["strategy"]:
            alerts.append("Trading strategy has encountered issues.")
            self.logger.warning("Trading strategy is not functioning properly.")

        if health_status["exchange_status"] != "ok":
            alerts.append(f"Exchange status is not ok: {health_status['exchange_status']}")
            self.logger.warning(f"Exchange status issue detected: {health_status['exchange_status']}")

        if alerts:
            self.logger.info(f"Bot health alerts generated: {alerts}")
            await self.notification_handler.async_send_notification(
                NotificationType.HEALTH_CHECK_ALERT,
                alert_details=" | ".join(alerts),
            )
        else:
            self.logger.info("Bot health is within acceptable parameters.")

    def _check_resource_usage(self) -> dict:
        """
        Collects detailed system and bot resource usage metrics.

        Returns:
            Dictionary containing various resource metrics.
        """
        # Get system-wide metrics
        cpu_percent = psutil.cpu_percent(interval=1)  # 1 second interval for accurate measurement
        virtual_memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        # Get process-specific metrics
        try:
            bot_memory_info = self.process.memory_info()
            bot_cpu_percent = self.process.cpu_percent()
            open_files = len(self.process.open_files())
            thread_count = self.process.num_threads()

            metrics = ResourceMetrics(
                timestamp=datetime.now(tz=UTC),
                cpu_percent=cpu_percent,
                memory_percent=virtual_memory.percent,
                disk_percent=disk.percent,
                bot_cpu_percent=bot_cpu_percent,
                bot_memory_mb=bot_memory_info.rss / (1024 * 1024),  # Convert to MB
                open_files=open_files,
                thread_count=thread_count,
            )

            # Store metrics history
            self._metrics_history.append(metrics)
            if len(self._metrics_history) > self.metrics_history_size:
                self._metrics_history.pop(0)

            return {
                "cpu": cpu_percent,
                "memory": virtual_memory.percent,
                "disk": disk.percent,
                "bot_cpu": bot_cpu_percent,
                "bot_memory_mb": bot_memory_info.rss / (1024 * 1024),
                "bot_memory_percent": (bot_memory_info.rss / virtual_memory.total) * 100,
                "open_files": open_files,
                "thread_count": thread_count,
                "memory_available_mb": virtual_memory.available / (1024 * 1024),
            }

        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            self.logger.error(f"Failed to get process metrics: {e}")
            return {
                "cpu": cpu_percent,
                "memory": virtual_memory.percent,
                "disk": disk.percent,
                "error": str(e),
            }

    def get_resource_trends(self) -> dict[str, float]:
        """
        Calculate resource usage trends over the stored history.

        Returns:
            Dictionary containing trend metrics (positive values indicate increasing usage).
        """
        if len(self._metrics_history) < 2:
            return {}

        recent = self._metrics_history[-1]
        old = self._metrics_history[0]
        time_diff = (recent.timestamp - old.timestamp).total_seconds() / 3600  # hours

        if time_diff < 0.016667:  # Less than 1 minute
            return {}

        return {
            "cpu_trend": (recent.cpu_percent - old.cpu_percent) / time_diff,
            "memory_trend": (recent.memory_percent - old.memory_percent) / time_diff,
            "bot_cpu_trend": (recent.bot_cpu_percent - old.bot_cpu_percent) / time_diff,
            "bot_memory_trend": (recent.bot_memory_mb - old.bot_memory_mb) / time_diff,
        }

    async def _check_and_alert_resource_usage(self, usage: dict):
        """
        Enhanced resource monitoring with trend analysis and detailed alerts.
        """
        alerts = []
        trends = self.get_resource_trends()

        # Check current values against thresholds
        for resource, threshold in RESOURCE_THRESHOLDS.items():
            current_value = usage.get(resource, 0)
            if current_value > threshold:
                trend = trends.get(f"{resource}_trend", 0)
                trend_direction = "increasing" if trend > 1 else "decreasing" if trend < -1 else "stable"
                message = (
                    f"{resource.upper()} usage is high: {current_value:.1f}% "
                    f"(Threshold: {threshold}%, Trend: {trend_direction})"
                )
                alerts.append(message)

        # Check for CPU spikes
        if trends.get("bot_cpu_trend", 0) > 10:  # %/hour
            alerts.append(f"High CPU usage trend: Bot CPU usage increasing by {trends['bot_cpu_trend']:.1f}%/hour")

        if alerts:
            self.logger.warning(f"Resource alerts: {alerts}")
            await self.notification_handler.async_send_notification(
                NotificationType.HEALTH_CHECK_ALERT,
                alert_details=" | ".join(alerts),
            )

    def _handle_stop(self, reason: str) -> None:
        """
        Handles the STOP_BOT event to stop the HealthCheck.

        Args:
            reason: The reason for stopping the bot.
        """
        if not self._is_running:
            self.logger.warning("HealthCheck is not running.")
            return

        self._is_running = False
        self._stop_event.set()
        self.logger.info(f"HealthCheck stopped: {reason}")

    async def _handle_start(self, reason: str) -> None:
        """
        Handles the START_BOT event to start the HealthCheck.

        Args:
            reason: The reason for starting the bot.
        """
        if self._is_running:
            self.logger.warning("HealthCheck is already running.")
            return

        self.logger.info(f"HealthCheck starting: {reason}")
        await self.start()
