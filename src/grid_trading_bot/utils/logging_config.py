import contextvars
import logging
from logging.handlers import RotatingFileHandler
import os

NO_BOT = "-"
current_bot: contextvars.ContextVar[str] = contextvars.ContextVar("current_bot", default=NO_BOT)

LOG_DIR = "logs"
MAX_FILE_SIZE = 5_000_000
BACKUP_COUNT = 5
CONSOLE_FORMAT = "%(asctime)s - [%(bot)s] %(name)s - %(levelname)s - %(message)s"
# Alloy parses this exact layout (monitoring/configs/alloy/config.alloy); a file holds one bot, so no bot field.
FILE_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class _BotFilter(logging.Filter):
    """Tags each record with the bot whose context emitted it and, with `only`, keeps that bot's and process records."""

    def __init__(self, only: str | None = None) -> None:
        super().__init__()
        self.only = only

    def filter(self, record: logging.LogRecord) -> bool:
        bot = getattr(record, "bot", None)
        if not isinstance(bot, str):
            bot = current_bot.get()
            record.bot = bot
        return self.only is None or bot in (self.only, NO_BOT)


_console_handler: logging.Handler | None = None
_bot_file_handlers: dict[str, RotatingFileHandler] = {}
_root_level_before: int | None = None


def _lower_root_level(level: int) -> None:
    global _root_level_before
    root = logging.getLogger()
    if _root_level_before is None:
        _root_level_before = root.level
    if level < root.level:
        root.setLevel(level)


def configure_console(level: int) -> None:
    """Installs the process's console handler on first call; later calls only lower its level and the root's."""
    global _console_handler
    if _console_handler is None:
        _console_handler = logging.StreamHandler()
        _console_handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        _console_handler.addFilter(_BotFilter())
        _console_handler.setLevel(level)
        logging.getLogger().addHandler(_console_handler)
    elif level < _console_handler.level:
        _console_handler.setLevel(level)
    _lower_root_level(level)
    logging.info(f"Logging initialized. Log level: {logging.getLevelName(level)}")


def add_bot_log_file(config_name: str, level: int) -> None:
    """Adds `logs/<config_name>.log`, which keeps the records of the bot running under that name and process records."""
    handler = _bot_file_handlers.get(config_name)
    if handler is None:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f"{config_name}.log")
        handler = RotatingFileHandler(path, maxBytes=MAX_FILE_SIZE, backupCount=BACKUP_COUNT)
        handler.setFormatter(logging.Formatter(FILE_FORMAT))
        handler.addFilter(_BotFilter(only=config_name))
        handler.setLevel(level)
        logging.getLogger().addHandler(handler)
        _bot_file_handlers[config_name] = handler
    elif level < handler.level:
        handler.setLevel(level)
    _lower_root_level(level)
    logging.info(f"File logging enabled. Logs are stored in: {os.path.join(LOG_DIR, f'{config_name}.log')}")


def reset_logging() -> None:
    """Removes and closes the handlers added by this module and restores the root level it found."""
    global _console_handler, _root_level_before
    root = logging.getLogger()
    handlers: list[logging.Handler] = list(_bot_file_handlers.values())
    if _console_handler is not None:
        handlers.append(_console_handler)
    for handler in handlers:
        root.removeHandler(handler)
        handler.close()
    _bot_file_handlers.clear()
    _console_handler = None
    if _root_level_before is not None:
        root.setLevel(_root_level_before)
        _root_level_before = None
