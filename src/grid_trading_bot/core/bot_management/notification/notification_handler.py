import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextvars
import logging
import string

import apprise

from grid_trading_bot.config.trading_mode import TradingMode

from .notification_content import NotificationType


class NotificationHandler:
    """
    Handles sending notifications through various channels using the Apprise library.
    Supports multiple notification services like Telegram, Discord, Slack, etc.
    """

    _executor = ThreadPoolExecutor(max_workers=3)

    def __init__(
        self,
        urls: list[str] | None,
        trading_mode: TradingMode,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.enabled = bool(urls) and trading_mode in {TradingMode.LIVE, TradingMode.PAPER_TRADING}
        if trading_mode in {TradingMode.LIVE, TradingMode.PAPER_TRADING} and not urls:
            self.logger.warning("Notifications are disabled: APPRISE_NOTIFICATION_URLS is empty.")
        self.lock = asyncio.Lock()
        self.apprise_instance = apprise.Apprise() if self.enabled else None
        if self.apprise_instance is not None:
            for url in urls or []:
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

            if not self.apprise_instance.notify(title=title, body=message):
                self.logger.error(f"Notification not delivered: {title}")

    async def async_send_notification(
        self,
        content: NotificationType | str,
        **kwargs,
    ) -> None:
        if not self.enabled:
            return
        async with self.lock:
            loop = asyncio.get_running_loop()
            try:
                context = contextvars.copy_context()
                await asyncio.wait_for(
                    loop.run_in_executor(
                        self._executor, context.run, lambda: self.send_notification(content, **kwargs)
                    ),
                    timeout=5,
                )
            except (TimeoutError, OSError, RuntimeError) as e:
                self.logger.error(f"Failed to send notification: {e!s}")
