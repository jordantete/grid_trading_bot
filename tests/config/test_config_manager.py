import copy
import json

import pytest

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.exceptions import ConfigFileNotFoundError, ConfigParseError, ConfigValidationError
from grid_trading_bot.config.schema import load_config
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.domain.spacing_type import SpacingType
from grid_trading_bot.core.domain.strategy_type import StrategyType
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

    def test_get_start_date(self, config_manager):
        assert config_manager.get_start_date() == "2024-07-04T00:00:00Z"

    def test_get_end_date(self, config_manager):
        assert config_manager.get_end_date() == "2024-07-11T00:00:00Z"

    def test_get_historical_data_file(self, config_manager):
        assert config_manager.get_historical_data_file() == "data/SOL_USDT/2024/1m.csv"

    def test_get_num_grids(self, config_manager):
        assert config_manager.get_num_grids() == 20

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

    def test_get_historical_data_file_default(self, valid_config):
        manager = _manager(valid_config, lambda raw: raw["trading_settings"].pop("historical_data_file"))
        assert manager.get_historical_data_file() is None

    @pytest.mark.parametrize("section", ["take_profit", "stop_loss"])
    def test_an_absent_take_profit_or_stop_loss_section_names_its_fields(self, valid_config, section):
        refusal = _refusal(valid_config, lambda raw: raw["risk_management"].pop(section))
        assert refusal.missing_fields == [f"risk_management.{section}.enabled", f"risk_management.{section}.threshold"]

    def test_get_buy_ratio_default(self, config_manager):
        assert config_manager.get_buy_ratio() == 1.0

    def test_get_sell_ratio_default(self, config_manager):
        assert config_manager.get_sell_ratio() == 1.0

    def test_get_buy_ratio_custom(self, valid_config):
        assert _manager(valid_config, lambda raw: raw["grid_strategy"].update(buy_ratio=0.8)).get_buy_ratio() == 0.8

    def test_get_sell_ratio_custom(self, valid_config):
        assert _manager(valid_config, lambda raw: raw["grid_strategy"].update(sell_ratio=0.5)).get_sell_ratio() == 0.5

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
        assert cm.get_exchange_name() == "binance"
        assert cm.get_trading_fee() == 0.001
        assert cm.get_base_currency() == "ETH"
        assert cm.get_quote_currency() == "USDT"

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


class TestTrailingStopLossAccessors:
    def test_defaults_when_section_absent(self, config_manager):
        assert config_manager.is_trailing_stop_loss_enabled() is False
        assert config_manager.get_trailing_atr_period() == 14
        assert config_manager.get_trailing_atr_multiplier() == 2.5
        assert config_manager.get_trailing_on_trigger() == "stop"

    def test_reads_configured_values(self, valid_config):
        def edit(raw):
            raw["grid_strategy"] |= {"spacing": "arithmetic", "dynamic_spacing": {"enabled": True}}
            raw["risk_management"]["trailing_stop_loss"] = {
                "enabled": True,
                "atr_period": 20,
                "atr_multiplier": 3.0,
                "on_trigger": "regrid",
            }

        config_manager = _manager(valid_config, edit)
        assert config_manager.is_trailing_stop_loss_enabled() is True
        assert config_manager.get_trailing_atr_period() == 20
        assert config_manager.get_trailing_atr_multiplier() == 3.0
        assert config_manager.get_trailing_on_trigger() == "regrid"


class TestDynamicSpacingAccessors:
    def test_defaults_when_section_absent(self, config_manager):
        assert config_manager.is_dynamic_spacing_enabled() is False
        assert config_manager.get_dynamic_atr_period() == 14
        assert config_manager.get_atr_spacing_multiplier() == 1.0
        assert config_manager.get_regrid_threshold() == 0.3
        assert config_manager.get_cooldown_bars() == 60

    def test_reads_configured_values(self, valid_config):
        def edit(raw):
            raw["grid_strategy"]["spacing"] = "arithmetic"
            raw["grid_strategy"]["dynamic_spacing"] = {
                "enabled": True,
                "atr_period": 10,
                "atr_spacing_multiplier": 1.5,
                "regrid_threshold": 0.5,
                "cooldown_bars": 120,
            }

        config_manager = _manager(valid_config, edit)
        assert config_manager.is_dynamic_spacing_enabled() is True
        assert config_manager.get_dynamic_atr_period() == 10
        assert config_manager.get_atr_spacing_multiplier() == 1.5
        assert config_manager.get_regrid_threshold() == 0.5
        assert config_manager.get_cooldown_bars() == 120


class TestPersistenceAccessors:
    def test_checkpoint_interval_default(self, config_manager):
        assert config_manager.get_checkpoint_interval_seconds() == 60.0

    def test_checkpoint_interval_configured(self, valid_config):
        config_manager = _manager(
            valid_config, lambda raw: raw.update(persistence={"checkpoint_interval_seconds": 30.0})
        )
        assert config_manager.get_checkpoint_interval_seconds() == 30.0


class TestConfigManagerFeeAndPostOnly:
    """Maker/taker fee split and the post-only execution flag."""

    def test_get_maker_fee_falls_back_to_trading_fee(self, config_manager):
        assert config_manager.get_maker_fee() == 0.001

    def test_get_taker_fee_falls_back_to_trading_fee(self, config_manager):
        assert config_manager.get_taker_fee() == 0.001

    def test_get_maker_fee_uses_explicit_value(self, valid_config):
        assert _manager(valid_config, lambda raw: raw["exchange"].update(maker_fee=0.0016)).get_maker_fee() == 0.0016

    def test_get_taker_fee_uses_explicit_value(self, valid_config):
        assert _manager(valid_config, lambda raw: raw["exchange"].update(taker_fee=0.0026)).get_taker_fee() == 0.0026

    def test_explicit_maker_fee_does_not_change_taker_fee(self, valid_config):
        assert _manager(valid_config, lambda raw: raw["exchange"].update(maker_fee=0.0)).get_taker_fee() == 0.001

    def test_get_post_only_defaults_to_false(self, config_manager):
        assert config_manager.get_post_only() is False

    def test_get_post_only_uses_explicit_value(self, valid_config):
        assert _manager(valid_config, lambda raw: raw["exchange"].update(post_only=True)).get_post_only() is True


class TestConfigManagerBacktestMinimums:
    def test_minimums_default_to_none(self, config_manager):
        assert config_manager.get_backtest_min_amount() is None
        assert config_manager.get_backtest_min_cost() is None

    def test_minimums_use_explicit_values(self, valid_config):
        config_manager = _manager(
            valid_config, lambda raw: raw.update(execution={"backtest_min_amount": 0.001, "backtest_min_cost": 5.0})
        )
        assert config_manager.get_backtest_min_amount() == 0.001
        assert config_manager.get_backtest_min_cost() == 5.0
