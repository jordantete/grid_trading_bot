import copy
import json

import pytest

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.exceptions import ConfigFileNotFoundError, ConfigParseError, ConfigValidationError
from grid_trading_bot.config.schema import load_config
from grid_trading_bot.core.persistence.serializers import compute_config_hash


def _edited(valid_config, edit):
    raw = copy.deepcopy(valid_config)
    edit(raw)
    return raw


def _manager(valid_config, edit=lambda raw: None) -> ConfigManager:
    return ConfigManager.from_dict(_edited(valid_config, edit))


def _refusal(valid_config, edit) -> ConfigValidationError:
    with pytest.raises(ConfigValidationError) as refused:
        _manager(valid_config, edit)
    return refused.value


@pytest.fixture
def config_manager(tmp_path, valid_config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(valid_config))
    return ConfigManager(str(path))


class TestConfigManager:
    def test_a_config_file_loads_into_the_schema_objects(self, config_manager, valid_config, tmp_path):
        assert config_manager.config == load_config(valid_config)
        assert config_manager.config_file == str(tmp_path / "config.json")

    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(ConfigFileNotFoundError):
            ConfigManager(str(tmp_path / "absent.json"))

    def test_a_malformed_file_is_refused(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text('{"invalid_json": ')
        with pytest.raises(ConfigParseError):
            ConfigManager(str(path))

    def test_an_invalid_config_file_is_refused(self, tmp_path, valid_config):
        path = tmp_path / "config.json"
        path.write_text(json.dumps(_edited(valid_config, lambda raw: raw["grid_strategy"].update(num_grids=0))))
        with pytest.raises(ConfigValidationError) as refused:
            ConfigManager(str(path))
        assert refused.value.invalid_fields == ["grid_strategy.num_grids"]

    def test_take_profit_and_stop_loss_load_into_typed_objects(self, config_manager):
        risk_management = config_manager.config.risk_management

        assert (risk_management.take_profit.enabled, risk_management.take_profit.threshold) == (False, 3700)
        assert (risk_management.stop_loss.enabled, risk_management.stop_loss.threshold) == (False, 2830)

    def test_the_log_level_loads_upper_case(self, config_manager):
        assert config_manager.config.logging.log_level == "INFO"

    def test_log_to_file_loads_from_the_config(self, config_manager):
        assert config_manager.config.logging.log_to_file is True

    @pytest.mark.parametrize(
        ("section", "key", "field"),
        [
            ("exchange", "trading_mode", "exchange.trading_mode"),
            ("grid_strategy", "spacing", "grid_strategy.spacing"),
            ("grid_strategy", "type", "grid_strategy.type"),
        ],
    )
    def test_an_unknown_enum_value_is_refused_at_load(self, valid_config, section, key, field):
        refusal = _refusal(valid_config, lambda raw: raw[section].update({key: "invalid_value"}))
        assert refusal.invalid_fields == [field]

    def test_an_absent_timeframe_is_refused_at_load(self, valid_config):
        refusal = _refusal(valid_config, lambda raw: raw["trading_settings"].pop("timeframe"))
        assert refusal.missing_fields == ["trading_settings.timeframe"]

    @pytest.mark.parametrize("section", ["take_profit", "stop_loss"])
    def test_an_absent_take_profit_or_stop_loss_section_names_its_fields(self, valid_config, section):
        refusal = _refusal(valid_config, lambda raw: raw["risk_management"].pop(section))
        assert refusal.missing_fields == [f"risk_management.{section}.enabled", f"risk_management.{section}.threshold"]

    @pytest.mark.parametrize(
        ("section", "key", "field"),
        [
            ("exchange", "name", "exchange.name"),
            ("exchange", "trading_mode", "exchange.trading_mode"),
            ("pair", "base_currency", "pair.base_currency"),
            ("pair", "quote_currency", "pair.quote_currency"),
            ("grid_strategy", "type", "grid_strategy.type"),
            ("grid_strategy", "spacing", "grid_strategy.spacing"),
            ("grid_strategy", "num_grids", "grid_strategy.num_grids"),
        ],
    )
    def test_an_absent_required_key_is_refused_at_load(self, valid_config, section, key, field):
        refusal = _refusal(valid_config, lambda raw: raw[section].pop(key))
        assert refusal.missing_fields == [field]

    @pytest.mark.parametrize(
        ("key", "field"),
        [
            ("start_date", "trading_settings.period.start_date"),
            ("end_date", "trading_settings.period.end_date"),
        ],
    )
    def test_an_absent_date_is_refused_at_load(self, valid_config, key, field):
        refusal = _refusal(valid_config, lambda raw: raw["trading_settings"]["period"].pop(key))
        assert refusal.missing_fields == [field]

    @pytest.mark.parametrize(
        ("section", "key", "field"),
        [
            ("exchange", "trading_mode", "exchange.trading_mode"),
            ("grid_strategy", "type", "grid_strategy.type"),
            ("grid_strategy", "spacing", "grid_strategy.spacing"),
        ],
    )
    def test_an_empty_enum_setting_is_refused_at_load(self, valid_config, section, key, field):
        refusal = _refusal(valid_config, lambda raw: raw[section].update({key: ""}))
        assert refusal.invalid_fields == [field]


class TestConfigManagerFromDict:
    def test_from_dict_creates_valid_config_manager(self, valid_config):
        cm = ConfigManager.from_dict(valid_config)
        assert cm.config == load_config(valid_config)

    def test_from_dict_validates_config(self):
        with pytest.raises(ConfigValidationError):
            ConfigManager.from_dict({"exchange": {}})

    def test_from_dict_sets_config_file_marker(self, valid_config):
        cm = ConfigManager.from_dict(valid_config)
        assert cm.config_file == "<dict>"


class TestConfigHash:
    def test_get_config_hash_is_the_hash_of_the_loaded_config(self, valid_config):
        assert ConfigManager.from_dict(valid_config).get_config_hash() == compute_config_hash(load_config(valid_config))


class TestStateDbPath:
    def test_the_default_path_is_keyed_on_the_pair_and_the_config_hash(self, valid_config):
        cm = ConfigManager.from_dict(valid_config)
        assert cm.get_state_db_path() == f"data/ETH_USDT/state_{cm.get_config_hash()[:8]}.db"

    def test_an_explicit_db_path_is_used_as_written(self, valid_config):
        cm = _manager(valid_config, lambda raw: raw.update(persistence={"db_path": "custom/state.db"}))
        assert cm.get_state_db_path() == "custom/state.db"

    def test_a_null_db_path_uses_the_default_path(self, valid_config):
        cm = _manager(valid_config, lambda raw: raw.update(persistence={"db_path": None}))
        assert cm.get_state_db_path() == f"data/ETH_USDT/state_{cm.get_config_hash()[:8]}.db"


class TestPersistenceSettings:
    def test_checkpoint_interval_default(self, config_manager):
        assert config_manager.config.persistence.checkpoint_interval_seconds == 60.0

    def test_checkpoint_interval_configured(self, valid_config):
        config_manager = _manager(
            valid_config, lambda raw: raw.update(persistence={"checkpoint_interval_seconds": 30.0})
        )
        assert config_manager.config.persistence.checkpoint_interval_seconds == 30.0


class TestConfigManagerBacktestMinimums:
    def test_minimums_default_to_none(self, config_manager):
        assert config_manager.config.execution.backtest_min_amount is None
        assert config_manager.config.execution.backtest_min_cost is None

    def test_minimums_use_explicit_values(self, valid_config):
        config_manager = _manager(
            valid_config, lambda raw: raw.update(execution={"backtest_min_amount": 0.001, "backtest_min_cost": 5.0})
        )
        assert config_manager.config.execution.backtest_min_amount == 0.001
        assert config_manager.config.execution.backtest_min_cost == 5.0


def test_an_integer_too_long_for_python_to_parse_is_a_parse_error(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text('{"grid_strategy": {"num_grids": ' + "9" * 5000 + "}}")

    with pytest.raises(ConfigParseError):
        ConfigManager(str(config_file))
