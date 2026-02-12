import asyncio
from dataclasses import dataclass, field
import logging
import os
from typing import Any

import click
from dotenv import load_dotenv

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.config_validator import ConfigValidator
from grid_trading_bot.config.exceptions import ConfigError
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.bot_controller.bot_controller import BotController
from grid_trading_bot.core.bot_management.event_bus import EventBus
from grid_trading_bot.core.bot_management.grid_trading_bot import GridTradingBot
from grid_trading_bot.core.bot_management.health_check import HealthCheck
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.utils.config_name_generator import generate_config_name
from grid_trading_bot.utils.logging_config import setup_logging
from grid_trading_bot.utils.performance_results_saver import save_or_append_performance_results


@dataclass
class ParsedArgs:
    config: list[str] = field(default_factory=list)
    save_performance_results: str | None = None
    no_plot: bool = False
    profile: bool = False


def validate_config_paths(ctx, param, value):
    for config_path in value:
        if not os.path.exists(config_path):
            raise click.BadParameter(f"Config file does not exist: {config_path}")
    return value


def validate_save_performance_results(ctx, param, value):
    if value is not None:
        save_dir = os.path.dirname(value)
        if save_dir and not os.path.exists(save_dir):
            raise click.BadParameter(f"The directory for saving performance results does not exist: {save_dir}")
    return value


def parse_and_validate_console_args(cli_args=None) -> ParsedArgs:
    try:
        return _run_command(cli_args, standalone_mode=False)

    except SystemExit as e:
        if e.code == 0:
            raise
        logging.error(f"Argument parsing failed: {e}")
        raise RuntimeError("Failed to parse arguments. Please check your inputs.") from e

    except click.BadParameter as e:
        logging.error(f"Validation failed: {e.format_message()}")
        raise RuntimeError("Argument validation failed.") from e

    except click.UsageError as e:
        logging.error(f"Argument parsing failed: {e.format_message()}")
        raise RuntimeError("Failed to parse arguments. Please check your inputs.") from e

    except Exception as e:
        logging.error(f"An unexpected error occurred while parsing arguments: {e}")
        raise RuntimeError("An unexpected error occurred during argument parsing.") from e


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


@click.group()
@click.version_option(package_name="grid_trading_bot")
def main():
    """Grid Trading Bot - Automate your grid trading strategy."""


@main.command()
@click.option(
    "--config",
    required=True,
    multiple=True,
    type=click.STRING,
    callback=validate_config_paths,
    help="Path(s) to the configuration file(s) containing strategy details.",
)
@click.option(
    "--save_performance_results",
    type=click.STRING,
    default=None,
    callback=validate_save_performance_results,
    help="Path to save simulation results (e.g., results.json).",
)
@click.option(
    "--no-plot",
    is_flag=True,
    default=False,
    help="Disable the display of plots at the end of the simulation.",
)
@click.option(
    "--profile",
    is_flag=True,
    default=False,
    help="Enable profiling for performance analysis.",
)
def run(config, save_performance_results, no_plot, profile):
    """Run the trading bot with the specified configuration."""

    async def _main():
        try:
            tasks = [run_bot(config_path, profile, save_performance_results, no_plot) for config_path in config]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for index, result in enumerate(results):
                if isinstance(result, Exception):
                    logging.error(
                        f"Error occurred while running bot for config {config[index]}: {result}",
                        exc_info=True,
                    )
                else:
                    if save_performance_results:
                        save_or_append_performance_results(result, save_performance_results)

        except Exception as e:
            logging.error(f"Critical error in main: {e}", exc_info=True)

        finally:
            await cleanup_tasks()
            logging.info("All tasks have completed.")

    asyncio.run(_main())


# Keep backward-compatible internal function used by arg_parser tests
_run_command = click.command(
    help=(
        "Spot Grid Trading Bot - Automate your grid trading strategy with confidence.\n\n"
        "This bot lets you automate your trading by implementing a grid strategy. "
        "Set your parameters, watch it execute, and manage your trades more effectively. "
        "Ideal for both beginners and experienced traders!"
    ),
)(
    click.option(
        "--config",
        required=True,
        multiple=True,
        type=click.STRING,
        callback=validate_config_paths,
        help="Path(s) to the configuration file(s) containing strategy details.",
    )(
        click.option(
            "--save_performance_results",
            type=click.STRING,
            default=None,
            callback=validate_save_performance_results,
            help="Path to save simulation results (e.g., results.json).",
        )(
            click.option(
                "--no-plot",
                is_flag=True,
                default=False,
                help="Disable the display of plots at the end of the simulation.",
            )(
                click.option(
                    "--profile",
                    is_flag=True,
                    default=False,
                    help="Enable profiling for performance analysis.",
                )(
                    lambda config, save_performance_results, no_plot, profile: ParsedArgs(
                        config=list(config),
                        save_performance_results=save_performance_results,
                        no_plot=no_plot,
                        profile=profile,
                    )
                )
            )
        )
    )
)
