from datetime import UTC, datetime, timedelta
from unittest.mock import mock_open, patch

import pytest

from grid_trading_bot.utils.performance_results_saver import save_or_append_performance_results


@pytest.fixture
def new_results_fixture():
    return {
        "config": "config.json",
        "performance_summary": {
            "start_time": datetime(2024, 12, 20, 10, 0, 0, tzinfo=UTC).isoformat(),
            "end_time": datetime(2024, 12, 20, 12, 0, 0, tzinfo=UTC).isoformat(),
            "total_profit": 500.0,
            "runtime": str(timedelta(hours=2)),
        },
        "orders": [
            [
                "BUY",
                "LIMIT",
                "FILLED",
                1000.0,
                0.5,
                datetime(2024, 12, 20, 10, 30, 0, tzinfo=UTC).isoformat(),
                "Level 1",
                0.1,
            ],
            [
                "SELL",
                "LIMIT",
                "FILLED",
                1500.0,
                0.5,
                datetime(2024, 12, 20, 11, 30, 0, tzinfo=UTC).isoformat(),
                "Level 2",
                0.05,
            ],
        ],
    }


def test_save_or_append_performance_results_invalid_json(new_results_fixture):
    with (
        patch("builtins.open", mock_open(read_data="INVALID_JSON")) as mocked_file,
        patch("os.path.exists", return_value=True),
        patch("grid_trading_bot.utils.performance_results_saver.logging.warning") as mock_logger_warning,
    ):
        save_or_append_performance_results(new_results_fixture, "results.json")

        mocked_file.assert_any_call("results.json")
        mocked_file.assert_any_call("results.json", "w")
        mock_logger_warning.assert_called_once_with("Could not decode JSON from results.json. Overwriting the file.")


def test_save_or_append_performance_results_os_error(new_results_fixture):
    with (
        patch("builtins.open", side_effect=OSError("Test OS Error")),
        patch("grid_trading_bot.utils.performance_results_saver.logging.error") as mock_logger_error,
    ):
        save_or_append_performance_results(new_results_fixture, "results.json")

        mock_logger_error.assert_called_once_with("Failed to save performance metrics to results.json: Test OS Error")


def test_save_or_append_performance_results_unexpected_exception(new_results_fixture):
    with (
        patch("builtins.open", side_effect=Exception("Unexpected Error")),
        patch("grid_trading_bot.utils.performance_results_saver.logging.error") as mock_logger_error,
    ):
        save_or_append_performance_results(new_results_fixture, "results.json")

        mock_logger_error.assert_any_call(
            "An unexpected error occurred while saving performance metrics: Unexpected Error",
        )
