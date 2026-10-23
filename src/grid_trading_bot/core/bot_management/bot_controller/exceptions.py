from grid_trading_bot.core.exceptions import GridBotError


class BotControllerError(GridBotError):
    """Base exception class for BotController errors."""


class CommandParsingError(BotControllerError):
    """Exception raised when there is an error parsing a command."""
