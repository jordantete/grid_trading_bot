import copy
from pathlib import Path
import re
from unittest.mock import patch

import pytest
from tests.fakes.config import config_manager_from

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.utils.config_name_generator import generate_config_name


@patch("grid_trading_bot.utils.config_name_generator.datetime")
def test_generate_config_name(mock_datetime, valid_config):
    mock_datetime.now.return_value.strftime.return_value = "20241220_1200"
    config_manager = config_manager_from(
        valid_config,
        {
            "exchange": {"trading_mode": "live"},
            "pair": {"base_currency": "BTC", "quote_currency": "USD"},
            "grid_strategy": {
                "type": "simple_grid",
                "spacing": "arithmetic",
                "num_grids": 10,
                "range": {"top": 50000, "bottom": 30000},
            },
        },
    )

    result = generate_config_name(config_manager)

    assert result == "bot_BTC_USD_LIVE_strategySIMPLE_GRID_spacingARITHMETIC_size10_range30000-50000_20241220_1200"


def test_generate_config_name_edge_cases(valid_config):
    config_manager = config_manager_from(
        valid_config,
        {
            "pair": {"base_currency": "ETH", "quote_currency": "EUR"},
            "grid_strategy": {"num_grids": 1, "range": {"top": 1, "bottom": 0}},
        },
    )

    result = generate_config_name(config_manager)

    assert "bot_ETH_EUR" in result
    assert "_size1_range0-1" in result


ALLOY_CONFIG = Path(__file__).parents[2] / "monitoring/configs/alloy/config.alloy"


def _alloy_filename_regex() -> re.Pattern[str]:
    expression = re.search(r'expression = "(/bot_.*\.log\$)"', ALLOY_CONFIG.read_text()).group(1)
    return re.compile(expression.replace("\\\\", "\\"))


@pytest.mark.parametrize(
    ("top", "bottom", "expected_range"),
    [
        (240, 210, "210-240"),
        (0.5, 0.25, "0.25-0.5"),
        (1_000_000, 1e-05, "0.00001-1000000"),
        (240.75, 210, "210-240.75"),
    ],
)
def test_the_name_feeds_the_alloy_filename_labels(valid_config, top, bottom, expected_range):
    config_manager = config_manager_from(
        valid_config,
        {
            "pair": {"base_currency": "SOL", "quote_currency": "USDT"},
            "grid_strategy": {"num_grids": 8, "range": {"top": top, "bottom": bottom}},
        },
    )

    name = generate_config_name(config_manager)

    match = _alloy_filename_regex().search(f"/app/logs/{name}.log")
    assert match is not None, name
    assert match.groupdict() == {
        "base_currency": "SOL",
        "quote_currency": "USDT",
        "trading_mode": "BACKTEST",
        "strategy_type": "SIMPLE_GRID",
        "spacing_type": "GEOMETRIC",
        "grid_size": "8",
        "grid_range": expected_range,
    }


def _without_range(raw):
    dynamic = copy.deepcopy(raw)
    del dynamic["grid_strategy"]["range"]
    dynamic["grid_strategy"]["spacing"] = "arithmetic"
    dynamic["grid_strategy"]["dynamic_spacing"] = {
        "enabled": True,
        "atr_period": 14,
        "atr_spacing_multiplier": 1.0,
        "regrid_threshold": 0.3,
        "cooldown_bars": 60,
    }
    return dynamic


def test_a_dynamic_grid_without_range_is_named_range_dynamic(valid_config):
    config_manager = ConfigManager.from_dict(_without_range(valid_config))

    assert "_rangeDynamic_" in generate_config_name(config_manager)


def test_the_alloy_regex_reads_a_dynamic_grid_name(valid_config):
    config_manager = ConfigManager.from_dict(_without_range(valid_config))

    name = generate_config_name(config_manager)
    match = _alloy_filename_regex().search(f"/app/logs/{name}.log")

    assert match is not None, name
    assert match.group("grid_range") == "Dynamic"
