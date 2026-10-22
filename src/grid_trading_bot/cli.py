import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import os
import sys
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
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.bot_management.process_lifecycle import ProcessLifecycle
from grid_trading_bot.core.bot_management.stop_request import EXIT_CONFIG, EXIT_FAILURE, EXIT_OK, aggregate_exit_codes
from grid_trading_bot.core.persistence.run_files import (
    RunFiles,
    RunLock,
    archive_state_db,
    read_halt_marker,
    remove_halt_marker,
)
from grid_trading_bot.utils.config_name_generator import generate_config_name
from grid_trading_bot.utils.logging_config import setup_logging
from grid_trading_bot.utils.performance_results_saver import save_or_append_performance_results


@dataclass(frozen=True)
class BotOutcome:
    exit_code: int
    result: dict[str, Any] | None = None


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
        sys.exit(EXIT_CONFIG)


def initialize_notification_handler(config_manager: ConfigManager) -> NotificationHandler:
    notification_urls_str = os.getenv("APPRISE_NOTIFICATION_URLS", "")
    notification_urls = [url.strip() for url in notification_urls_str.split(",") if url.strip()]
    trading_mode = config_manager.get_trading_mode()
    return NotificationHandler(notification_urls, trading_mode)


async def run_bot(
    config_path: str,
    config_manager: ConfigManager,
    lifecycle: ProcessLifecycle,
    profile: bool = False,
    save_performance_results_path: str | None = None,
    no_plot: bool = False,
) -> BotOutcome:
    config_name = generate_config_name(config_manager)
    setup_logging(config_manager.get_logging_level(), config_manager.should_log_to_file(), config_name)
    event_bus = EventBus()
    notification_handler = initialize_notification_handler(config_manager)
    live_or_paper = config_manager.get_trading_mode() in {TradingMode.LIVE, TradingMode.PAPER_TRADING}
    run_files = RunFiles.for_config(config_manager) if live_or_paper else None
    run_lock = RunLock(run_files.lock_path) if run_files is not None else None

    if run_lock is not None and not run_lock.acquire():
        logging.error(f"Another process is already running {config_path} (lock file {run_lock.path}).")
        return BotOutcome(EXIT_FAILURE)
    try:
        if run_files is not None:
            halt = read_halt_marker(run_files.halt_path)
            if halt is not None:
                logging.warning(
                    f"Trading on {config_path} was halted on {halt.halted_at} ({halt.kind}): {halt.reason}. "
                    f"Run `grid_trading_bot resume --config {config_path}` to start a new grid.",
                )
                await notification_handler.async_send_notification(
                    NotificationType.START_REFUSED_HALTED,
                    halted_at=halt.halted_at,
                    kind=halt.kind,
                    reason=halt.reason,
                    config_path=config_path,
                )
                return BotOutcome(EXIT_OK)
        return await _run_bot(
            config_path,
            config_manager,
            lifecycle,
            event_bus,
            notification_handler,
            run_files,
            profile,
            save_performance_results_path,
            no_plot,
        )
    finally:
        if run_lock is not None:
            run_lock.release()


async def _run_bot(
    config_path: str,
    config_manager: ConfigManager,
    lifecycle: ProcessLifecycle,
    event_bus: EventBus,
    notification_handler: NotificationHandler,
    run_files: RunFiles | None,
    profile: bool,
    save_performance_results_path: str | None,
    no_plot: bool,
) -> BotOutcome:
    bot = GridTradingBot(
        config_path,
        config_manager,
        notification_handler,
        event_bus,
        save_performance_results_path,
        no_plot,
        run_files=run_files,
    )
    bot_controller = BotController(bot, event_bus)
    health_check = HealthCheck(
        bot, notification_handler, event_bus, check_interval=config_manager.get_health_check_interval()
    )

    if profile:
        import cProfile

        cProfile.runctx("asyncio.run(bot.run())", globals(), locals(), "profile_results.prof")
        return BotOutcome(EXIT_OK)

    result = None
    crashed = False
    try:
        if run_files is not None:
            lifecycle.register(bot)
            background = [asyncio.create_task(health_check.start(), name="HealthCheckTask")]
            if sys.stdin.isatty():
                background.append(asyncio.create_task(bot_controller.command_listener(), name="BotControllerTask"))
            else:
                logging.info(
                    "No terminal attached: command listener disabled. "
                    "SIGTERM suspends the bot (orders kept), SIGUSR1 halts it (orders cancelled).",
                )
            try:
                result = await bot.run()
            finally:
                lifecycle.unregister(bot)
                for task in background:
                    task.cancel()
                await asyncio.gather(*background, return_exceptions=True)
        else:
            result = await bot.run()

    except Exception as e:
        crashed = True
        logging.error(f"An unexpected error occurred: {e}", exc_info=True)

    finally:
        try:
            bot_controller.cleanup()
            health_check.cleanup()
            await event_bus.shutdown()

        except Exception as e:
            logging.error(f"Error during shutdown: {e}", exc_info=True)

    if bot.stop_request is not None:
        return BotOutcome(bot.stop_request.exit_code, result)
    return BotOutcome(EXIT_FAILURE if crashed else EXIT_OK, result)


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
    config_managers = [initialize_config(config_path) for config_path in config]
    live_or_paper = any(
        config_manager.get_trading_mode() in {TradingMode.LIVE, TradingMode.PAPER_TRADING}
        for config_manager in config_managers
    )

    async def _main() -> int:
        lifecycle = ProcessLifecycle()
        if live_or_paper:
            lifecycle.install(asyncio.get_running_loop())
        try:
            outcomes = await asyncio.gather(
                *(
                    run_bot(config_path, config_manager, lifecycle, profile, save_performance_results, no_plot)
                    for config_path, config_manager in zip(config, config_managers, strict=True)
                ),
                return_exceptions=True,
            )
            exit_codes = []
            for config_path, outcome in zip(config, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    logging.error(
                        f"Error occurred while running bot for config {config_path}: {outcome}", exc_info=outcome
                    )
                    exit_codes.append(EXIT_FAILURE)
                    continue
                exit_codes.append(outcome.exit_code)
                if save_performance_results and outcome.result is not None:
                    save_or_append_performance_results(outcome.result, save_performance_results)
            return aggregate_exit_codes(exit_codes)

        finally:
            lifecycle.uninstall()
            await cleanup_tasks()
            logging.info("All tasks have completed.")

    sys.exit(asyncio.run(_main()))


@main.command()
@click.option(
    "--config",
    required=True,
    type=click.STRING,
    callback=lambda ctx, param, value: validate_config_paths(ctx, param, [value])[0],
    help="Path to the configuration file of the halted bot.",
)
def resume(config):
    """Clear a recorded halt: the next start is a fresh grid on the current exchange balances."""
    run_files = RunFiles.for_config(initialize_config(config))
    run_lock = RunLock(run_files.lock_path)
    if not run_lock.acquire():
        click.echo(f"A bot is running {config} (lock file {run_files.lock_path}); stop it first.", err=True)
        sys.exit(EXIT_FAILURE)
    try:
        halt = read_halt_marker(run_files.halt_path)
        if halt is None:
            click.echo(f"No halt recorded for {config}: the next start resumes the saved state.")
            return
        moved = archive_state_db(run_files.db_path, datetime.now(UTC))
        remove_halt_marker(run_files.halt_path)
        click.echo(f"Halt cleared for {config} (was {halt.kind}: {halt.reason}).")
        for path in moved:
            click.echo(f"State moved to {path}")
    finally:
        run_lock.release()


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
