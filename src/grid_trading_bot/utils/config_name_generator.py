from datetime import UTC, datetime
from decimal import Decimal

from grid_trading_bot.config.config_manager import ConfigManager


def _bound(value: float | None) -> str:
    if value is None:
        return "None"
    if float(value).is_integer():
        return str(int(value))
    return format(Decimal(repr(float(value))), "f")


def generate_config_name(config_manager: ConfigManager) -> str:
    """
    Generates a unique and descriptive name for the bot's configuration.

    Args:
        config_manager (ConfigManager): Config manager instance to retrieve key parameters.

    Returns:
        str: A descriptive configuration name including trading pair, mode, strategy,
        grid spacing, grid range, and timestamp.
    """
    trading_pair = f"{config_manager.config.pair.base_currency}_{config_manager.config.pair.quote_currency}"
    trading_mode = config_manager.config.exchange.trading_mode.name
    grid_strategy = config_manager.config.grid_strategy
    grid_strategy_type = grid_strategy.type.name
    grid_spacing_type = grid_strategy.spacing.name
    grid_size = grid_strategy.num_grids
    grid_range = grid_strategy.range
    grid_top = _bound(None if grid_range is None else grid_range.top)
    grid_bottom = _bound(None if grid_range is None else grid_range.bottom)
    start_time = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M")

    return (
        f"bot_{trading_pair}_{trading_mode}_strategy{grid_strategy_type}_"
        f"spacing{grid_spacing_type}_size{grid_size}_range{grid_bottom}-{grid_top}_{start_time}"
    )
