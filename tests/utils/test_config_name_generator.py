from unittest.mock import patch

from tests.fakes.config import config_manager_from

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

    assert result == "bot_BTC_USD_LIVE_strategySIMPLE_GRID_spacingARITHMETIC_size10_range30000.0-50000.0_20241220_1200"


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
    assert "_size1_range0.0-1.0" in result
