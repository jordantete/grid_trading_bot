import json
from unittest.mock import Mock, mock_open, patch

import pytest

from config.config_manager import ConfigManager
from config.config_validator import ConfigValidator
from config.exceptions import ConfigFileNotFoundError, ConfigParseError
from config.trading_mode import TradingMode
from core.domain.spacing_type import SpacingType
from core.domain.strategy_type import StrategyType


class TestConfigManager:
    @pytest.fixture
    def mock_validator(self):
        return Mock(spec=ConfigValidator)

    @pytest.fixture
    def config_manager(self, mock_validator, valid_config):
        # Mocking both open and os.path.exists to simulate a valid config file
        mocked_open = mock_open(read_data=json.dumps(valid_config))
        with patch("builtins.open", mocked_open), patch("os.path.exists", return_value=True):
            return ConfigManager("config.json", mock_validator)

    def test_load_config_valid(self, config_manager, valid_config, mock_validator):
        mock_validator.validate.assert_called_once_with(valid_config)
        assert config_manager.config == valid_config

    def test_load_config_file_not_found(self, mock_validator):
        with patch("os.path.exists", return_value=False), pytest.raises(ConfigFileNotFoundError):
            ConfigManager("config.json", mock_validator)

    def test_load_config_json_decode_error(self, mock_validator):
        invalid_json = '{"invalid_json": '  # Malformed JSON
        mocked_open = mock_open(read_data=invalid_json)
        with (
            patch("builtins.open", mocked_open),
            patch("os.path.exists", return_value=True),
            pytest.raises(ConfigParseError),
        ):
            ConfigManager("config.json", mock_validator)

    def test_get_exchange_name(self, config_manager):
        assert config_manager.get_exchange_name() == "binance"

    def test_get_trading_fee(self, config_manager):
        assert config_manager.get_trading_fee() == 0.001

    def test_get_base_currency(self, config_manager):
        assert config_manager.get_base_currency() == "ETH"

    def test_get_quote_currency(self, config_manager):
        assert config_manager.get_quote_currency() == "USDT"

    def test_get_initial_balance(self, config_manager):
        assert config_manager.get_initial_balance() == 10000

    def test_get_spacing_type(self, config_manager):
        assert config_manager.get_spacing_type() == SpacingType.GEOMETRIC

    def test_get_strategy_type(self, config_manager):
        assert config_manager.get_strategy_type() == StrategyType.SIMPLE_GRID

    def test_get_trading_mode(self, config_manager):
        assert config_manager.get_trading_mode() == TradingMode.BACKTEST

    def test_get_timeframe(self, config_manager):
        assert config_manager.get_timeframe() == "1m"

    def test_get_period(self, config_manager):
        expected_period = {
            "start_date": "2024-07-04T00:00:00Z",
            "end_date": "2024-07-11T00:00:00Z",
        }
        assert config_manager.get_period() == expected_period

    def test_get_start_date(self, config_manager):
        assert config_manager.get_start_date() == "2024-07-04T00:00:00Z"

    def test_get_end_date(self, config_manager):
        assert config_manager.get_end_date() == "2024-07-11T00:00:00Z"

    def test_get_num_grids(self, config_manager):
        assert config_manager.get_num_grids() == 20

    def test_get_grid_range(self, config_manager):
        expected_range = {
            "top": 3100,
            "bottom": 2850,
        }
        assert config_manager.get_grid_range() == expected_range

    def test_get_top_range(self, config_manager):
        assert config_manager.get_top_range() == 3100

    def test_get_bottom_range(self, config_manager):
        assert config_manager.get_bottom_range() == 2850

    def test_is_take_profit_enabled(self, config_manager):
        assert not config_manager.is_take_profit_enabled()

    def test_get_take_profit_threshold(self, config_manager):
        assert config_manager.get_take_profit_threshold() == 3700

    def test_get_stop_loss_threshold(self, config_manager):
        assert config_manager.get_stop_loss_threshold() == 2830

    def test_is_stop_loss_enabled(self, config_manager):
        assert not config_manager.is_stop_loss_enabled()

    def test_get_log_level(self, config_manager):
        assert config_manager.get_logging_level() == "INFO"

    def test_should_log_to_file_true(self, config_manager):
        assert config_manager.should_log_to_file() is True

    def test_get_trading_mode_invalid_value(self, config_manager):
        config_manager.config["exchange"]["trading_mode"] = "invalid_mode"

        with pytest.raises(
            ValueError,
            match="Invalid trading mode: 'invalid_mode'. Available modes are: backtest, paper_trading, live",
        ):
            config_manager.get_trading_mode()

    def test_get_spacing_type_invalid_value(self, config_manager):
        config_manager.config["grid_strategy"]["spacing"] = "invalid_spacing"

        with pytest.raises(
            ValueError,
            match="Invalid spacing type: 'invalid_spacing'. Available spacings are: arithmetic, geometric",
        ):
            config_manager.get_spacing_type()

    def test_get_strategy_type_invalid_value(self, config_manager):
        config_manager.config["grid_strategy"]["type"] = "invalid_strategy"

        with pytest.raises(
            ValueError,
            match="Invalid strategy type: 'invalid_strategy'. Available strategies are: simple_grid, hedged_grid",
        ):
            config_manager.get_strategy_type()

    def test_get_timeframe_default(self, config_manager):
        del config_manager.config["trading_settings"]["timeframe"]
        assert config_manager.get_timeframe() == "1h"

    def test_get_historical_data_file_default(self, config_manager):
        del config_manager.config["trading_settings"]["historical_data_file"]
        assert config_manager.get_historical_data_file() is None

    def test_is_take_profit_enabled_default(self, config_manager):
        del config_manager.config["risk_management"]["take_profit"]
        assert config_manager.is_take_profit_enabled() is False

    def test_get_take_profit_threshold_default(self, config_manager):
        del config_manager.config["risk_management"]["take_profit"]
        assert config_manager.get_take_profit_threshold() is None

    def test_is_stop_loss_enabled_default(self, config_manager):
        del config_manager.config["risk_management"]["stop_loss"]
        assert config_manager.is_stop_loss_enabled() is False

    def test_get_stop_loss_threshold_default(self, config_manager):
        del config_manager.config["risk_management"]["stop_loss"]
        assert config_manager.get_stop_loss_threshold() is None
