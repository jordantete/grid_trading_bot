from datetime import UTC, datetime

from grid_trading_bot.config.config_manager import ConfigManager


def generate_config_name(config_manager: ConfigManager) -> str:
    """
    Generates a unique and descriptive name for the bot's configuration.

    Args:
        config_manager (ConfigManager): Config manager instance to retrieve key parameters.

    Returns:
        str: A descriptive configuration name including trading pair, mode, strategy,
        grid spacing, grid range, and timestamp.
    """
    trading_pair = f"{config_manager.get_base_currency()}_{config_manager.get_quote_currency()}"
    trading_mode = config_manager.get_trading_mode().name
    grid_strategy_type = config_manager.get_strategy_type().name
    grid_spacing_type = config_manager.get_spacing_type().name
    grid_size = config_manager.get_num_grids()
    grid_top = config_manager.get_top_range()
    grid_bottom = config_manager.get_bottom_range()
    start_time = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M")

    return (
        f"bot_{trading_pair}_{trading_mode}_strategy{grid_strategy_type}_"
        f"spacing{grid_spacing_type}_size{grid_size}_range{grid_bottom}-{grid_top}_{start_time}"
    )
