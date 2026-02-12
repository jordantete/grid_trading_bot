import asyncio
import logging
import os
from typing import Any

from dotenv import load_dotenv

from config.config_manager import ConfigManager
from config.config_validator import ConfigValidator
from config.exceptions import ConfigError
from config.trading_mode import TradingMode
from core.bot_management.bot_controller.bot_controller import BotController
from core.bot_management.event_bus import EventBus
from core.bot_management.grid_trading_bot import GridTradingBot
from core.bot_management.health_check import HealthCheck
from core.bot_management.notification.notification_handler import NotificationHandler
from utils.arg_parser import parse_and_validate_console_args
from utils.config_name_generator import generate_config_name
from utils.logging_config import setup_logging
from utils.performance_results_saver import save_or_append_performance_results


def initialize_config(config_path: str) -> ConfigManager:
    load_dotenv()
    try:
        return ConfigManager(config_path, ConfigValidator())

    except ConfigError as e:
        logging.error(f"An error occurred during the initialization of ConfigManager {e}")
        exit(1)


def initialize_notification_handler(config_manager: ConfigManager, event_bus: EventBus) -> NotificationHandler:
    notification_urls_str = os.getenv("APPRISE_NOTIFICATION_URLS", "")
    notification_urls = [url.strip() for url in notification_urls_str.split(",") if url.strip()]
    trading_mode = config_manager.get_trading_mode()
    return NotificationHandler(event_bus, notification_urls, trading_mode)


async def run_bot(
    config_path: str,
    profile: bool = False,
    save_performance_results_path: str | None = None,
    no_plot: bool = False,
) -> dict[str, Any] | None:
    config_manager = initialize_config(config_path)
    config_name = generate_config_name(config_manager)
    setup_logging(config_manager.get_logging_level(), config_manager.should_log_to_file(), config_name)
    event_bus = EventBus()
    notification_handler = initialize_notification_handler(config_manager, event_bus)
    bot = GridTradingBot(
        config_path,
        config_manager,
        notification_handler,
        event_bus,
        save_performance_results_path,
        no_plot,
    )
    bot_controller = BotController(bot, event_bus)
    health_check = HealthCheck(bot, notification_handler, event_bus)

    if profile:
        import cProfile

        cProfile.runctx("asyncio.run(bot.run())", globals(), locals(), "profile_results.prof")
        return None

    try:
        if bot.trading_mode in {TradingMode.LIVE, TradingMode.PAPER_TRADING}:
            bot_task = asyncio.create_task(bot.run(), name="BotTask")
            bot_controller_task = asyncio.create_task(bot_controller.command_listener(), name="BotControllerTask")
            health_check_task = asyncio.create_task(health_check.start(), name="HealthCheckTask")
            await asyncio.gather(bot_task, bot_controller_task, health_check_task)
        else:
            await bot.run()

    except asyncio.CancelledError:
        logging.info("Cancellation received. Shutting down gracefully.")

    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}", exc_info=True)

    finally:
        try:
            await event_bus.shutdown()

        except Exception as e:
            logging.error(f"Error during EventBus shutdown: {e}", exc_info=True)


async def cleanup_tasks():
    logging.info("Shutting down bot and cleaning up tasks...")

    current_task = asyncio.current_task()
    tasks_to_cancel = {
        task for task in asyncio.all_tasks() if task is not current_task and not task.done() and not task.cancelled()
    }

    logging.info(f"Tasks to cancel: {len(tasks_to_cancel)}")

    for task in tasks_to_cancel:
        logging.info(f"Task to cancel: {task} - Done: {task.done()} - Cancelled: {task.cancelled()}")
        task.cancel()

    try:
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

    except asyncio.CancelledError:
        logging.info("Tasks cancelled successfully.")

    except Exception as e:
        logging.error(f"Error during task cancellation: {e}", exc_info=True)


if __name__ == "__main__":
    args = parse_and_validate_console_args()

    async def main():
        try:
            tasks = [
                run_bot(config_path, args.profile, args.save_performance_results, args.no_plot)
                for config_path in args.config
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for index, result in enumerate(results):
                if isinstance(result, Exception):
                    logging.error(
                        f"Error occurred while running bot for config {args.config[index]}: {result}",
                        exc_info=True,
                    )
                else:
                    if args.save_performance_results:
                        save_or_append_performance_results(result, args.save_performance_results)

        except Exception as e:
            logging.error(f"Critical error in main: {e}", exc_info=True)

        finally:
            await cleanup_tasks()
            logging.info("All tasks have completed.")

    asyncio.run(main())
