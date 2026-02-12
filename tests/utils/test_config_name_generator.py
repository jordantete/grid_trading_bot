from unittest.mock import Mock, patch

import pytest

from grid_trading_bot.utils.config_name_generator import generate_config_name


@pytest.fixture
def mock_config_manager():
    mock_manager = Mock()
    mock_manager.get_base_currency.return_value = "BTC"
    mock_manager.get_quote_currency.return_value = "USD"
    mock_manager.get_trading_mode.return_value.name = "LIVE"
    mock_manager.get_strategy_type.return_value.name = "GRID"
    mock_manager.get_spacing_type.return_value.name = "PERCENTAGE"
    mock_manager.get_num_grids.return_value = 10
    mock_manager.get_top_range.return_value = 50000
    mock_manager.get_bottom_range.return_value = 30000
    return mock_manager


@patch("grid_trading_bot.utils.config_name_generator.datetime")
def test_generate_config_name(mock_datetime, mock_config_manager):
    mock_datetime.now.return_value.strftime.return_value = "20241220_1200"

    result = generate_config_name(mock_config_manager)

    expected_name = "bot_BTC_USD_LIVE_strategyGRID_spacingPERCENTAGE_size10_range30000-50000_20241220_1200"
    assert result == expected_name
    mock_config_manager.get_base_currency.assert_called_once()
    mock_config_manager.get_quote_currency.assert_called_once()
    mock_config_manager.get_trading_mode.assert_called_once()
    mock_config_manager.get_strategy_type.assert_called_once()
    mock_config_manager.get_spacing_type.assert_called_once()
    mock_config_manager.get_num_grids.assert_called_once()
    mock_config_manager.get_top_range.assert_called_once()
    mock_config_manager.get_bottom_range.assert_called_once()


def test_generate_config_name_edge_cases(mock_config_manager):
    mock_config_manager.get_base_currency.return_value = "ETH"
    mock_config_manager.get_quote_currency.return_value = "EUR"
    mock_config_manager.get_num_grids.return_value = 0
    mock_config_manager.get_top_range.return_value = 0
    mock_config_manager.get_bottom_range.return_value = 0

    result = generate_config_name(mock_config_manager)

    assert "bot_ETH_EUR" in result
    assert "_size0_range0-0" in result
