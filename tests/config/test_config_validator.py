import pytest

from grid_trading_bot.config.config_validator import ConfigValidator
from grid_trading_bot.config.exceptions import ConfigValidationError


@pytest.fixture
def config_validator():
    return ConfigValidator()


class TestConfigValidator:
    def test_validate_valid_config(self, config_validator, valid_config):
        try:
            config_validator.validate(valid_config)
        except ConfigValidationError:
            pytest.fail("Valid configuration raised ConfigValidationError")

    def test_validate_missing_required_fields(self, config_validator):
        invalid_config = {
            "exchange": {},
            "pair": {},
            "trading_settings": {},
            "grid_strategy": {},
            "risk_management": {},
            "logging": {},
        }
        with pytest.raises(ConfigValidationError) as excinfo:
            config_validator.validate(invalid_config)

        missing_fields = excinfo.value.missing_fields
        invalid_fields = excinfo.value.invalid_fields

        assert "pair.base_currency" in missing_fields
        assert "pair.quote_currency" in missing_fields
        assert "trading_settings.initial_balance" in missing_fields
        assert "trading_settings.period.start_date" in missing_fields
        assert "trading_settings.period.end_date" in missing_fields
        assert "grid_strategy.num_grids" in missing_fields
        assert "grid_strategy.range.top" in missing_fields
        assert "grid_strategy.range.bottom" in missing_fields
        assert "logging.log_level" in missing_fields

        assert "exchange.name" in invalid_fields
        assert "exchange.trading_fee" in invalid_fields
        assert "trading_settings.timeframe" in invalid_fields

    def test_validate_invalid_exchange(self, config_validator, valid_config):
        valid_config["exchange"] = {"name": "", "trading_fee": -0.01}  # Invalid exchange
        with pytest.raises(ConfigValidationError) as excinfo:
            config_validator.validate(valid_config)
        assert "exchange.name" in excinfo.value.invalid_fields
        assert "exchange.trading_fee" in excinfo.value.invalid_fields

    def test_validate_valid_trading_modes(self, config_validator, valid_config):
        for mode in ["live", "paper_trading", "backtest"]:
            valid_config["exchange"]["trading_mode"] = mode
            try:
                config_validator.validate(valid_config)
            except ConfigValidationError:
                pytest.fail(f"Valid trading_mode '{mode}' raised ConfigValidationError")

    def test_validate_invalid_trading_mode(self, config_validator, valid_config):
        valid_config["exchange"]["trading_mode"] = "invalid_mode"
        with pytest.raises(ConfigValidationError, match=r"exchange.trading_mode"):
            config_validator.validate(valid_config)

    def test_validate_invalid_timeframe(self, config_validator, valid_config):
        valid_config["trading_settings"]["timeframe"] = "3h"  # Invalid timeframe
        with pytest.raises(ConfigValidationError) as excinfo:
            config_validator.validate(valid_config)
        assert "trading_settings.timeframe" in excinfo.value.invalid_fields

    def test_validate_missing_period_fields(self, config_validator, valid_config):
        valid_config["trading_settings"]["period"] = {}  # Missing start and end date
        with pytest.raises(ConfigValidationError) as excinfo:
            config_validator.validate(valid_config)
        assert "trading_settings.period.start_date" in excinfo.value.missing_fields
        assert "trading_settings.period.end_date" in excinfo.value.missing_fields

    def test_validate_invalid_grid_settings(self, config_validator, valid_config):
        # Test invalid grid type
        valid_config["grid_strategy"]["type"] = "invalid_type"  # Invalid grid type
        with pytest.raises(ConfigValidationError) as excinfo:
            config_validator.validate(valid_config)
        assert "grid_strategy.type" in excinfo.value.invalid_fields

        # Test missing num_grids
        valid_config["grid_strategy"]["num_grids"] = None
        with pytest.raises(ConfigValidationError) as excinfo:
            config_validator.validate(valid_config)
        assert "grid_strategy.num_grids" in excinfo.value.missing_fields

        # Test invalid top/bottom range (bottom should be less than top)
        valid_config["grid_strategy"]["range"] = {"top": 2800, "bottom": 2850}  # Invalid range
        with pytest.raises(ConfigValidationError) as excinfo:
            config_validator.validate(valid_config)
        assert "grid_strategy.range.top" in excinfo.value.invalid_fields
        assert "grid_strategy.range.bottom" in excinfo.value.invalid_fields

    def test_validate_limits_invalid_type(self, config_validator, valid_config):
        valid_config["risk_management"] = {
            "take_profit": {"enabled": "yes"},  # Invalid boolean
            "stop_loss": {"enabled": 1},  # Invalid boolean
        }
        with pytest.raises(ConfigValidationError) as excinfo:
            config_validator.validate(valid_config)
        assert "risk_management.take_profit.enabled" in excinfo.value.invalid_fields
        assert "risk_management.stop_loss.enabled" in excinfo.value.invalid_fields

    def test_validate_logging_invalid_level(self, config_validator, valid_config):
        valid_config["logging"] = {
            "log_level": "VERBOSE",  # Invalid log level
            "log_to_file": True,
        }
        with pytest.raises(ConfigValidationError) as excinfo:
            config_validator.validate(valid_config)
        assert "logging.log_level" in excinfo.value.invalid_fields

    def test_validate_logging_missing_level(self, config_validator, valid_config):
        valid_config["logging"] = {"log_to_file": True}
        with pytest.raises(ConfigValidationError) as excinfo:
            config_validator.validate(valid_config)
        assert "logging.log_level" in excinfo.value.missing_fields

    def test_validate_ratios_valid(self, config_validator, valid_config):
        valid_config["grid_strategy"]["buy_ratio"] = 1.0
        valid_config["grid_strategy"]["sell_ratio"] = 0.5
        try:
            config_validator.validate(valid_config)
        except ConfigValidationError:
            pytest.fail("Valid ratios raised ConfigValidationError")

    def test_validate_ratios_absent_is_valid(self, config_validator, valid_config):
        assert "buy_ratio" not in valid_config["grid_strategy"]
        assert "sell_ratio" not in valid_config["grid_strategy"]
        try:
            config_validator.validate(valid_config)
        except ConfigValidationError:
            pytest.fail("Config without ratios raised ConfigValidationError")

    @pytest.mark.parametrize("ratio_field", ["buy_ratio", "sell_ratio"])
    @pytest.mark.parametrize("invalid_value", [0, -0.5, 1.5, "abc"])
    def test_validate_ratio_invalid_values(self, config_validator, valid_config, ratio_field, invalid_value):
        valid_config["grid_strategy"][ratio_field] = invalid_value
        with pytest.raises(ConfigValidationError) as excinfo:
            config_validator.validate(valid_config)
        assert f"grid_strategy.{ratio_field}" in excinfo.value.invalid_fields


class TestTrailingStopLossValidation:
    def test_absent_section_is_valid(self, config_validator, valid_config):
        config_validator.validate(valid_config)  # must not raise

    def test_invalid_on_trigger_rejected(self, config_validator, valid_config):
        valid_config["risk_management"]["trailing_stop_loss"] = {
            "enabled": True,
            "atr_period": 14,
            "atr_multiplier": 2.5,
            "on_trigger": "explode",
        }
        with pytest.raises(ConfigValidationError):
            config_validator.validate(valid_config)

    def test_regrid_trigger_requires_dynamic_spacing(self, config_validator, valid_config):
        valid_config["risk_management"]["trailing_stop_loss"] = {
            "enabled": True,
            "atr_period": 14,
            "atr_multiplier": 2.5,
            "on_trigger": "regrid",
        }
        # dynamic_spacing absent => invalid
        with pytest.raises(ConfigValidationError):
            config_validator.validate(valid_config)

    def test_atr_period_below_two_rejected(self, config_validator, valid_config):
        valid_config["risk_management"]["trailing_stop_loss"] = {
            "enabled": True,
            "atr_period": 1,
            "atr_multiplier": 2.5,
            "on_trigger": "stop",
        }
        with pytest.raises(ConfigValidationError):
            config_validator.validate(valid_config)


class TestDynamicSpacingValidation:
    def _enable_dynamic(self, config, **overrides):
        config["grid_strategy"]["spacing"] = "arithmetic"
        section = {
            "enabled": True,
            "atr_period": 14,
            "atr_spacing_multiplier": 1.0,
            "regrid_threshold": 0.3,
            "cooldown_bars": 60,
        }
        section.update(overrides)
        config["grid_strategy"]["dynamic_spacing"] = section

    def test_valid_dynamic_spacing_accepted(self, config_validator, valid_config):
        self._enable_dynamic(valid_config)
        config_validator.validate(valid_config)  # must not raise

    def test_geometric_spacing_rejected_with_dynamic(self, config_validator, valid_config):
        self._enable_dynamic(valid_config)
        valid_config["grid_strategy"]["spacing"] = "geometric"
        with pytest.raises(ConfigValidationError):
            config_validator.validate(valid_config)

    def test_range_optional_when_dynamic_enabled(self, config_validator, valid_config):
        self._enable_dynamic(valid_config)
        del valid_config["grid_strategy"]["range"]
        config_validator.validate(valid_config)  # must not raise

    def test_range_still_required_when_dynamic_disabled(self, config_validator, valid_config):
        del valid_config["grid_strategy"]["range"]
        with pytest.raises(ConfigValidationError):
            config_validator.validate(valid_config)

    def test_negative_threshold_rejected(self, config_validator, valid_config):
        self._enable_dynamic(valid_config, regrid_threshold=-0.1)
        with pytest.raises(ConfigValidationError):
            config_validator.validate(valid_config)
