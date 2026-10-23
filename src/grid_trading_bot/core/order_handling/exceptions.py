from grid_trading_bot.core.exceptions import GridBotError


class GridFeasibilityError(GridBotError):
    """Raised when the configured grid produces orders below exchange minimums."""
