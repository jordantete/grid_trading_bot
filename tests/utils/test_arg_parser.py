from unittest.mock import patch

import pytest

from grid_trading_bot.cli import parse_and_validate_console_args


@pytest.mark.parametrize(
    ("args", "expected_config"),
    [
        (["--config", "config1.json"], ["config1.json"]),
        (["--config", "config1.json", "--config", "config2.json"], ["config1.json", "config2.json"]),
    ],
)
@patch("os.path.exists", return_value=True)
def test_parse_and_validate_console_args_required(mock_exists, args, expected_config):
    result = parse_and_validate_console_args(args)
    assert result.config == expected_config


@patch("os.path.exists", return_value=True)
def test_parse_and_validate_console_args_save_performance_results_exists(mock_exists):
    result = parse_and_validate_console_args(["--config", "config.json", "--save_performance_results", "results.json"])
    assert result.save_performance_results == "results.json"


def test_parse_and_validate_console_args_save_performance_results_dir_does_not_exist():
    with (
        patch("os.path.exists", side_effect=lambda path: path == "config.json"),
        patch("grid_trading_bot.cli.logging.error") as mock_log,
    ):
        with pytest.raises(RuntimeError, match="Argument validation failed."):
            parse_and_validate_console_args(
                ["--config", "config.json", "--save_performance_results", "non_existent_dir/results.json"]
            )
        mock_log.assert_called_once()
        assert "does not exist" in mock_log.call_args[0][0]


@patch("os.path.exists", return_value=True)
def test_parse_and_validate_console_args_no_plot(mock_exists):
    result = parse_and_validate_console_args(["--config", "config.json", "--no-plot"])
    assert result.no_plot is True


@patch("os.path.exists", return_value=True)
def test_parse_and_validate_console_args_profile(mock_exists):
    result = parse_and_validate_console_args(["--config", "config.json", "--profile"])
    assert result.profile is True


@patch("grid_trading_bot.cli.logging.error")
def test_parse_and_validate_console_args_argument_error(mock_log):
    with pytest.raises(RuntimeError, match="Failed to parse arguments. Please check your inputs."):
        parse_and_validate_console_args(["--config"])
    mock_log.assert_called_once()


@patch("grid_trading_bot.cli.logging.error")
def test_parse_and_validate_console_args_unexpected_error(mock_log):
    with patch("os.path.exists", side_effect=Exception("Unexpected error")):
        with pytest.raises(RuntimeError, match="An unexpected error occurred during argument parsing."):
            parse_and_validate_console_args(["--config", "config.json", "--save_performance_results", "results.json"])
        mock_log.assert_any_call("An unexpected error occurred while parsing arguments: Unexpected error")
