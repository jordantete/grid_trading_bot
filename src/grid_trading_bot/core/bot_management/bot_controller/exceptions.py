class BotControllerError(Exception):
    """Base exception class for BotController errors."""


class CommandParsingError(BotControllerError):
    """Exception raised when there is an error parsing a command."""


class StrategyControlError(BotControllerError):
    """Exception raised when starting, stopping, or restarting the strategy fails."""
