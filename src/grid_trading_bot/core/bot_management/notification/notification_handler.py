import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import string

import apprise

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.order_handling.order import Order

from .notification_content import NotificationType


class NotificationHandler:
    """
    Handles sending notifications through various channels using the Apprise library.
    Supports multiple notification services like Telegram, Discord, Slack, etc.
    """

    _executor = ThreadPoolExecutor(max_workers=3)

    def __init__(
        self,
        event_bus: EventBus,
        urls: list[str] | None,
        trading_mode: TradingMode,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.event_bus = event_bus
        self.enabled = bool(urls) and trading_mode in {TradingMode.LIVE, TradingMode.PAPER_TRADING}
        self.lock = asyncio.Lock()
        self.apprise_instance = apprise.Apprise() if self.enabled else None

        if self.enabled and urls is not None:
            self.event_bus.subscribe(Events.ORDER_FILLED, self._send_notification_on_order_filled)

            for url in urls:
                self.apprise_instance.add(url)

    _formatter = string.Formatter()

    def send_notification(
        self,
        content: NotificationType | str,
        **kwargs,
    ) -> None:
        if self.enabled and self.apprise_instance:
            if isinstance(content, NotificationType):
                title = content.value.title
                message_template = content.value.message
                required_placeholders = {
                    field_name
                    for _, field_name, _, _ in self._formatter.parse(message_template)
                    if field_name is not None
                }
                missing_placeholders = required_placeholders - kwargs.keys()

                if missing_placeholders:
                    self.logger.warning(
                        f"Missing placeholders for notification: {missing_placeholders}. "
                        "Defaulting to 'N/A' for missing values.",
                    )

                message = message_template.format(**{key: kwargs.get(key, "N/A") for key in required_placeholders})
            else:
                title = "Notification"
                message = content

            self.apprise_instance.notify(title=title, body=message)

    async def async_send_notification(
        self,
        content: NotificationType | str,
        **kwargs,
    ) -> None:
        async with self.lock:
            loop = asyncio.get_running_loop()
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(self._executor, lambda: self.send_notification(content, **kwargs)),
                    timeout=5,
                )
            except Exception as e:
                self.logger.error(f"Failed to send notification: {e!s}")

    async def _send_notification_on_order_filled(self, order: Order) -> None:
        await self.async_send_notification(NotificationType.ORDER_FILLED, order_details=str(order))
