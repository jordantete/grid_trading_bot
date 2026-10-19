class GridBotError(Exception):
    """Root of every exception the bot raises on purpose."""


class OrderRefusedError(GridBotError):
    """An order refused for a business reason, by the bot's own checks or by the exchange: an expected outcome."""
