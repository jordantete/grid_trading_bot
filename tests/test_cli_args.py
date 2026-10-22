"""Option parsing of the real `run` command: make_context runs the options and their callbacks, not the command body."""

from unittest.mock import patch

import click
import pytest

from grid_trading_bot.cli import run


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    return str(path)


def _params(args):
    return run.make_context("run", list(args)).params


def test_one_config_is_parsed(config_file):
    params = _params(["--config", config_file])

    assert params == {"config": (config_file,), "save_performance_results": None, "no_plot": False, "profile": False}


def test_several_configs_are_parsed_in_order(tmp_path, config_file):
    second = tmp_path / "second.json"
    second.write_text("{}")

    assert _params(["--config", config_file, "--config", str(second)])["config"] == (config_file, str(second))


def test_a_missing_config_file_is_refused(tmp_path):
    missing = str(tmp_path / "missing.json")

    with pytest.raises(click.BadParameter, match=f"Config file does not exist: {missing}"):
        _params(["--config", missing])


def test_a_results_path_in_an_existing_directory_is_kept(tmp_path, config_file):
    results = str(tmp_path / "results.json")

    assert _params(["--config", config_file, "--save_performance_results", results])["save_performance_results"] == (
        results
    )


def test_a_results_path_in_a_missing_directory_is_refused(tmp_path, config_file):
    missing_dir = str(tmp_path / "non_existent_dir")

    with pytest.raises(
        click.BadParameter, match=f"The directory for saving performance results does not exist: {missing_dir}"
    ):
        _params(["--config", config_file, "--save_performance_results", f"{missing_dir}/results.json"])


def test_no_plot_is_a_flag(config_file):
    assert _params(["--config", config_file, "--no-plot"])["no_plot"] is True


def test_profile_is_a_flag(config_file):
    assert _params(["--config", config_file, "--profile"])["profile"] is True


def test_config_without_a_value_is_a_usage_error():
    with pytest.raises(click.UsageError, match="'--config' requires an argument"):
        _params(["--config"])


def test_config_is_required():
    with pytest.raises(click.MissingParameter) as missing:
        _params([])

    assert missing.value.param.name == "config"


def test_an_unexpected_error_in_a_check_propagates_unchanged(config_file):
    with (
        patch("grid_trading_bot.cli.os.path.exists", side_effect=OSError("disk gone")),
        pytest.raises(OSError, match="disk gone"),
    ):
        _params(["--config", config_file])
