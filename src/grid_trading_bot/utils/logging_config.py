import logging
from logging.handlers import RotatingFileHandler
import os


def setup_logging(
    log_level: int,
    log_to_file: bool = False,
    config_name: str | None = None,
    max_file_size: int = 5_000_000,  # 5MB default max file size for rotation
    backup_count: int = 5,  # Default number of backup files
) -> None:
    """
    Sets up logging with options for console, rotating file logging, and log differentiation.

    Args:
        log_level (int): The logging level (e.g., logging.INFO, logging.DEBUG).
        log_to_file (bool): Whether to log to a file.
        config_name (str | None): Name of the bot configuration to differentiate logs.
        max_file_size (int): Maximum size of log file in bytes before rotation.
        backup_count (int): Number of backup log files to keep.
    """
    handlers = []

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    handlers.append(console_handler)
    log_file_path = ""

    if log_to_file:
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)

        if config_name:
            log_file_path = os.path.join(log_dir, f"{config_name}.log")
        else:
            log_file_path = os.path.join(log_dir, "grid_trading_bot.log")

        file_handler = RotatingFileHandler(log_file_path, maxBytes=max_file_size, backupCount=backup_count)
        file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        handlers.append(file_handler)

    logging.basicConfig(level=log_level, handlers=handlers)
    logging.info(f"Logging initialized. Log level: {logging.getLevelName(log_level)}")

    if log_to_file:
        logging.info(f"File logging enabled. Logs are stored in: {log_file_path}")
