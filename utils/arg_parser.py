from dataclasses import dataclass, field
import logging
import os

import click


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


@click.command(
    help=(
        "Spot Grid Trading Bot - Automate your grid trading strategy with confidence.\n\n"
        "This bot lets you automate your trading by implementing a grid strategy. "
        "Set your parameters, watch it execute, and manage your trades more effectively. "
        "Ideal for both beginners and experienced traders!"
    ),
)
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
def cli(config, save_performance_results, no_plot, profile):
    return ParsedArgs(
        config=list(config),
        save_performance_results=save_performance_results,
        no_plot=no_plot,
        profile=profile,
    )


def parse_and_validate_console_args(cli_args=None) -> ParsedArgs:
    try:
        return cli(cli_args, standalone_mode=False)

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
