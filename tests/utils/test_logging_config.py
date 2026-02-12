import logging
from unittest.mock import MagicMock, patch

import pytest

from grid_trading_bot.utils.logging_config import setup_logging


@pytest.fixture
def mock_makedirs():
    with patch("os.makedirs") as mocked_makedirs:
        yield mocked_makedirs


@pytest.fixture
def mock_basic_config():
    with patch("logging.basicConfig") as mocked_basic_config:
        yield mocked_basic_config


@pytest.fixture
def mock_rotating_file_handler():
    with patch("logging.handlers.RotatingFileHandler") as mocked_handler:
        mocked_handler.return_value = MagicMock()
        yield mocked_handler


def test_setup_logging_console_only(mock_basic_config):
    setup_logging(log_level=logging.INFO, log_to_file=False)

    mock_basic_config.assert_called_once()
    handlers = mock_basic_config.call_args[1]["handlers"]
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)


@patch("os.makedirs")
@patch("logging.basicConfig")
@patch("logging.handlers.RotatingFileHandler")
def test_setup_logging_file_logging(mock_rotating_file_handler, mock_basic_config, mock_makedirs):
    setup_logging(
        log_level=logging.DEBUG,
        log_to_file=True,
        config_name="test_config",
        max_file_size=10_000_000,
        backup_count=3,
    )

    mock_makedirs.assert_called_once_with("logs", exist_ok=True)
    mock_basic_config.assert_called_once()
    handlers = mock_basic_config.call_args[1]["handlers"]

    assert len(handlers) == 2
    assert any(isinstance(handler, logging.StreamHandler) for handler in handlers)


@patch("os.makedirs")
@patch("logging.basicConfig")
@patch("logging.handlers.RotatingFileHandler")
def test_setup_logging_default_file_logging(mock_rotating_file_handler, mock_basic_config, mock_makedirs):
    setup_logging(log_level=logging.WARNING, log_to_file=True)

    mock_makedirs.assert_called_once_with("logs", exist_ok=True)
    mock_basic_config.assert_called_once()
    handlers = mock_basic_config.call_args[1]["handlers"]

    assert len(handlers) == 2
    assert any(isinstance(handler, logging.StreamHandler) for handler in handlers)


def test_setup_logging_logs_info(mock_basic_config, mock_rotating_file_handler, caplog):
    with caplog.at_level(logging.INFO):
        setup_logging(log_level=logging.INFO, log_to_file=True, config_name="test_config")

    assert "Logging initialized. Log level: INFO" in caplog.text
    assert "File logging enabled. Logs are stored in: logs/test_config.log" in caplog.text


def test_setup_logging_directory_creation_error(mock_makedirs):
    mock_makedirs.side_effect = OSError("Directory creation failed")

    with pytest.raises(OSError, match="Directory creation failed"):
        setup_logging(log_level=logging.DEBUG, log_to_file=True)
