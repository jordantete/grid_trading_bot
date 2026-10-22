from collections.abc import Callable
import copy
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from grid_trading_bot.config.exceptions import ConfigValidationError
from grid_trading_bot.config.schema import (
    BotConfig,
    DynamicSpacingConfig,
    ExecutionConfig,
    PersistenceConfig,
    RangeConfig,
    TrailingStopLossConfig,
    TrailingTrigger,
    load_config,
)
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.domain.spacing_type import SpacingType
from grid_trading_bot.core.domain.strategy_type import StrategyType

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
OLD_GETTER_VALUES = Path(__file__).resolve().parent / "fixtures" / "old_getter_values.json"


def _refused(raw: dict[str, Any]) -> ConfigValidationError:
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(raw)
    return excinfo.value


def _set(raw: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        raw = raw[key]
    raw[path[-1]] = value


def _enable_dynamic(config: dict[str, Any], **overrides: Any) -> None:
    config["grid_strategy"]["spacing"] = "arithmetic"
    config["grid_strategy"]["dynamic_spacing"] = {
        "enabled": True,
        "atr_period": 14,
        "atr_spacing_multiplier": 1.0,
        "regrid_threshold": 0.3,
        "cooldown_bars": 60,
        **overrides,
    }


def test_a_valid_config_loads(valid_config):
    config = load_config(valid_config)

    assert isinstance(config, BotConfig)
    assert config.grid_strategy.num_grids == 20
    assert config.exchange.trading_mode is TradingMode.BACKTEST
    assert config.grid_strategy.type is StrategyType.SIMPLE_GRID
    assert config.grid_strategy.spacing is SpacingType.GEOMETRIC
    assert config.grid_strategy.range == RangeConfig(top=3100.0, bottom=2850.0)
    assert config.trading_settings.period.start_date == "2024-07-04T00:00:00Z"
    assert config.execution == ExecutionConfig()
    assert config.persistence == PersistenceConfig()


def test_an_empty_config_names_every_missing_field():
    error = _refused(
        {
            "exchange": {},
            "pair": {},
            "trading_settings": {},
            "grid_strategy": {},
            "risk_management": {},
            "logging": {},
        }
    )

    assert set(error.missing_fields) == {
        "exchange.name",
        "exchange.trading_fee",
        "exchange.trading_mode",
        "pair.base_currency",
        "pair.quote_currency",
        "trading_settings.initial_balance",
        "trading_settings.timeframe",
        "trading_settings.period.start_date",
        "trading_settings.period.end_date",
        "grid_strategy.type",
        "grid_strategy.spacing",
        "grid_strategy.num_grids",
        "grid_strategy.range.top",
        "grid_strategy.range.bottom",
        "risk_management.take_profit.enabled",
        "risk_management.take_profit.threshold",
        "risk_management.stop_loss.enabled",
        "risk_management.stop_loss.threshold",
        "logging.log_level",
        "logging.log_to_file",
    }
    assert error.invalid_fields == []


def test_an_absent_top_level_section_is_a_missing_field_named_after_it(valid_config):
    del valid_config["pair"]
    del valid_config["logging"]

    error = _refused(valid_config)

    assert error.missing_fields == ["pair", "logging"]
    assert error.invalid_fields == []


def test_an_empty_exchange_name_and_a_negative_fee_are_refused(valid_config):
    valid_config["exchange"] = {"name": "", "trading_fee": -0.01, "trading_mode": "backtest"}

    error = _refused(valid_config)

    assert "exchange.name" in error.invalid_fields
    assert "exchange.trading_fee" in error.invalid_fields


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("live", TradingMode.LIVE), ("paper_trading", TradingMode.PAPER_TRADING), ("backtest", TradingMode.BACKTEST)],
)
def test_every_trading_mode_loads(valid_config, mode, expected):
    valid_config["exchange"]["trading_mode"] = mode

    assert load_config(valid_config).exchange.trading_mode is expected


def test_an_unknown_trading_mode_is_refused(valid_config):
    valid_config["exchange"]["trading_mode"] = "invalid_mode"

    assert _refused(valid_config).invalid_fields == ["exchange.trading_mode"]


def test_an_unknown_timeframe_is_refused(valid_config):
    valid_config["trading_settings"]["timeframe"] = "3h"

    assert _refused(valid_config).invalid_fields == ["trading_settings.timeframe"]


def test_an_absent_historical_data_file_loads_as_none(valid_config):
    del valid_config["trading_settings"]["historical_data_file"]

    assert load_config(valid_config).trading_settings.historical_data_file is None


def test_an_empty_period_names_both_dates_as_missing(valid_config):
    valid_config["trading_settings"]["period"] = {}

    assert _refused(valid_config).missing_fields == [
        "trading_settings.period.start_date",
        "trading_settings.period.end_date",
    ]


class TestGridSettings:
    def test_the_range_bounds_are_read(self, valid_config):
        grid_range = load_config(valid_config).grid_strategy.range

        assert (grid_range.top, grid_range.bottom) == (3100, 2850)

    def test_an_unknown_grid_type_is_refused(self, valid_config):
        valid_config["grid_strategy"]["type"] = "invalid_type"

        assert _refused(valid_config).invalid_fields == ["grid_strategy.type"]

    def test_a_null_num_grids_is_refused(self, valid_config):
        valid_config["grid_strategy"]["num_grids"] = None

        assert _refused(valid_config).invalid_fields == ["grid_strategy.num_grids"]

    def test_an_absent_num_grids_is_missing(self, valid_config):
        del valid_config["grid_strategy"]["num_grids"]

        assert _refused(valid_config).missing_fields == ["grid_strategy.num_grids"]

    @pytest.mark.parametrize("num_grids", [0, -3, 2.5])
    def test_a_num_grids_that_is_not_a_positive_integer_is_refused(self, valid_config, num_grids):
        valid_config["grid_strategy"]["num_grids"] = num_grids

        assert _refused(valid_config).invalid_fields == ["grid_strategy.num_grids"]

    @pytest.mark.parametrize(("top", "bottom"), [(2800, 2850), (2850, 2850)])
    def test_a_bottom_at_or_above_the_top_makes_both_bounds_invalid(self, valid_config, top, bottom):
        valid_config["grid_strategy"]["range"] = {"top": top, "bottom": bottom}

        assert _refused(valid_config).invalid_fields == ["grid_strategy.range.top", "grid_strategy.range.bottom"]

    @pytest.mark.parametrize("bound", ["top", "bottom"])
    def test_a_range_with_one_bound_is_refused(self, valid_config, bound):
        other = "bottom" if bound == "top" else "top"
        del valid_config["grid_strategy"]["range"][other]

        error = _refused(valid_config)

        assert error.missing_fields == [f"grid_strategy.range.{other}"]
        assert error.invalid_fields == []


def test_a_take_profit_and_stop_loss_flag_that_is_not_a_boolean_is_refused(valid_config):
    valid_config["risk_management"] = {
        "take_profit": {"enabled": "yes", "threshold": 3700},
        "stop_loss": {"enabled": 1, "threshold": 2830},
    }

    assert _refused(valid_config).invalid_fields == [
        "risk_management.take_profit.enabled",
        "risk_management.stop_loss.enabled",
    ]


def test_a_threshold_is_required_even_when_its_exit_is_disabled(valid_config):
    del valid_config["risk_management"]["take_profit"]["threshold"]

    assert _refused(valid_config).missing_fields == ["risk_management.take_profit.threshold"]


class TestLogging:
    def test_an_unknown_log_level_is_refused(self, valid_config):
        valid_config["logging"] = {"log_level": "VERBOSE", "log_to_file": True}

        assert _refused(valid_config).invalid_fields == ["logging.log_level"]

    def test_an_absent_log_level_is_missing(self, valid_config):
        valid_config["logging"] = {"log_to_file": True}

        assert _refused(valid_config).missing_fields == ["logging.log_level"]

    def test_a_log_level_in_any_case_is_stored_upper_case(self, valid_config):
        valid_config["logging"]["log_level"] = "debug"

        assert load_config(valid_config).logging.log_level == "DEBUG"

    def test_a_log_level_that_is_not_a_string_is_refused(self, valid_config):
        valid_config["logging"]["log_level"] = 10

        assert _refused(valid_config).invalid_fields == ["logging.log_level"]


class TestRatios:
    def test_valid_ratios_load(self, valid_config):
        valid_config["grid_strategy"]["buy_ratio"] = 1.0
        valid_config["grid_strategy"]["sell_ratio"] = 0.5

        grid = load_config(valid_config).grid_strategy

        assert (grid.buy_ratio, grid.sell_ratio) == (1.0, 0.5)

    def test_absent_ratios_default_to_one(self, valid_config):
        assert "buy_ratio" not in valid_config["grid_strategy"]
        assert "sell_ratio" not in valid_config["grid_strategy"]

        grid = load_config(valid_config).grid_strategy

        assert (grid.buy_ratio, grid.sell_ratio) == (1.0, 1.0)

    @pytest.mark.parametrize("ratio_field", ["buy_ratio", "sell_ratio"])
    @pytest.mark.parametrize("invalid_value", [0, -0.5, 1.5, "abc"])
    def test_a_ratio_outside_zero_exclusive_to_one_is_refused(self, valid_config, ratio_field, invalid_value):
        valid_config["grid_strategy"][ratio_field] = invalid_value

        assert _refused(valid_config).invalid_fields == [f"grid_strategy.{ratio_field}"]


class TestTrailingStopLoss:
    def test_an_absent_section_loads_disabled_with_its_defaults(self, valid_config):
        trailing = load_config(valid_config).risk_management.trailing_stop_loss

        assert trailing == TrailingStopLossConfig(enabled=False)
        assert (trailing.atr_period, trailing.atr_multiplier, trailing.on_trigger) == (14, 2.5, TrailingTrigger.STOP)

    def test_an_unknown_on_trigger_is_refused(self, valid_config):
        valid_config["risk_management"]["trailing_stop_loss"] = {
            "enabled": True,
            "atr_period": 14,
            "atr_multiplier": 2.5,
            "on_trigger": "explode",
        }

        assert _refused(valid_config).invalid_fields == ["risk_management.trailing_stop_loss.on_trigger"]

    def test_a_regrid_trigger_without_dynamic_spacing_is_refused(self, valid_config):
        valid_config["risk_management"]["trailing_stop_loss"] = {
            "enabled": True,
            "atr_period": 14,
            "atr_multiplier": 2.5,
            "on_trigger": "regrid",
        }

        assert _refused(valid_config).invalid_fields == ["risk_management.trailing_stop_loss.on_trigger"]

    def test_a_regrid_trigger_with_dynamic_spacing_loads(self, valid_config):
        _enable_dynamic(valid_config)
        valid_config["risk_management"]["trailing_stop_loss"] = {"enabled": True, "on_trigger": "regrid"}

        assert load_config(valid_config).risk_management.trailing_stop_loss.on_trigger is TrailingTrigger.REGRID

    def test_an_atr_period_below_two_is_refused(self, valid_config):
        valid_config["risk_management"]["trailing_stop_loss"] = {
            "enabled": True,
            "atr_period": 1,
            "atr_multiplier": 2.5,
            "on_trigger": "stop",
        }

        assert _refused(valid_config).invalid_fields == ["risk_management.trailing_stop_loss.atr_period"]

    def test_a_present_section_without_its_flag_is_refused(self, valid_config):
        valid_config["risk_management"]["trailing_stop_loss"] = {"atr_period": 14}

        assert _refused(valid_config).missing_fields == ["risk_management.trailing_stop_loss.enabled"]


class TestDynamicSpacing:
    def test_an_absent_section_loads_with_the_defaults(self, valid_config):
        assert load_config(valid_config).grid_strategy.dynamic_spacing == DynamicSpacingConfig(
            enabled=False, atr_period=14, atr_spacing_multiplier=1.0, regrid_threshold=0.3, cooldown_bars=60
        )

    def test_every_configured_value_is_read(self, valid_config):
        _enable_dynamic(
            valid_config, atr_period=10, atr_spacing_multiplier=1.5, regrid_threshold=0.5, cooldown_bars=120
        )

        assert load_config(valid_config).grid_strategy.dynamic_spacing == DynamicSpacingConfig(
            enabled=True, atr_period=10, atr_spacing_multiplier=1.5, regrid_threshold=0.5, cooldown_bars=120
        )

    def test_a_valid_section_loads(self, valid_config):
        _enable_dynamic(valid_config, atr_spacing_multiplier=0.5)

        assert load_config(valid_config).grid_strategy.dynamic_spacing == DynamicSpacingConfig(
            enabled=True, atr_period=14, atr_spacing_multiplier=0.5, regrid_threshold=0.3, cooldown_bars=60
        )

    def test_geometric_spacing_is_refused_with_dynamic_spacing(self, valid_config):
        _enable_dynamic(valid_config)
        valid_config["grid_strategy"]["spacing"] = "geometric"

        assert _refused(valid_config).invalid_fields == ["grid_strategy.spacing"]

    def test_the_range_is_optional_when_dynamic_spacing_is_enabled(self, valid_config):
        _enable_dynamic(valid_config)
        del valid_config["grid_strategy"]["range"]

        assert load_config(valid_config).grid_strategy.range is None

    def test_the_range_is_required_when_dynamic_spacing_is_disabled(self, valid_config):
        del valid_config["grid_strategy"]["range"]

        assert _refused(valid_config).missing_fields == ["grid_strategy.range.top", "grid_strategy.range.bottom"]

    def test_a_negative_regrid_threshold_is_refused(self, valid_config):
        _enable_dynamic(valid_config, regrid_threshold=-0.1)

        assert _refused(valid_config).invalid_fields == ["grid_strategy.dynamic_spacing.regrid_threshold"]

    def test_a_present_section_without_its_flag_is_refused(self, valid_config):
        valid_config["grid_strategy"]["dynamic_spacing"] = {"atr_period": 14}

        assert "grid_strategy.dynamic_spacing.enabled" in _refused(valid_config).missing_fields


class TestMakerTakerAndPostOnly:
    @pytest.mark.parametrize("field", ["maker_fee", "taker_fee"])
    def test_a_negative_fee_is_refused(self, valid_config, field):
        valid_config["exchange"][field] = -0.001

        assert _refused(valid_config).invalid_fields == [f"exchange.{field}"]

    def test_a_non_numeric_maker_fee_is_refused(self, valid_config):
        valid_config["exchange"]["maker_fee"] = "0.001"

        assert _refused(valid_config).invalid_fields == ["exchange.maker_fee"]

    def test_a_null_maker_fee_is_refused(self, valid_config):
        valid_config["exchange"]["maker_fee"] = None

        assert _refused(valid_config).invalid_fields == ["exchange.maker_fee"]

    def test_a_non_boolean_post_only_is_refused(self, valid_config):
        valid_config["exchange"]["post_only"] = "yes"

        assert _refused(valid_config).invalid_fields == ["exchange.post_only"]

    def test_a_zero_maker_fee_loads(self, valid_config):
        valid_config["exchange"].update(maker_fee=0.0, taker_fee=0.0004, post_only=True)

        exchange = load_config(valid_config).exchange

        assert (exchange.maker_fee, exchange.taker_fee, exchange.post_only) == (0.0, 0.0004, True)

    def test_explicit_maker_and_taker_fees_are_read(self, valid_config):
        valid_config["exchange"].update(maker_fee=0.0016, taker_fee=0.0026)

        exchange = load_config(valid_config).exchange

        assert (exchange.maker_fee, exchange.taker_fee) == (0.0016, 0.0026)

    def test_a_maker_fee_alone_leaves_the_taker_fee_on_the_trading_fee(self, valid_config):
        valid_config["exchange"]["maker_fee"] = 0.0

        assert load_config(valid_config).exchange.taker_fee == 0.001

    def test_absent_maker_and_taker_fees_fall_back_to_the_trading_fee(self, valid_config):
        exchange = load_config(valid_config).exchange

        assert (exchange.maker_fee, exchange.taker_fee, exchange.post_only) == (0.001, 0.001, False)


class TestBacktestMarketMinimums:
    @pytest.mark.parametrize("field", ["backtest_min_amount", "backtest_min_cost"])
    def test_a_non_negative_number_loads(self, valid_config, field):
        valid_config["execution"] = {field: 5.0}

        assert getattr(load_config(valid_config).execution, field) == 5.0

    @pytest.mark.parametrize("field", ["backtest_min_amount", "backtest_min_cost"])
    @pytest.mark.parametrize("value", [-1.0, "5", True, float("nan"), float("inf")])
    def test_an_invalid_value_is_refused(self, valid_config, field, value):
        valid_config["execution"] = {field: value}

        assert _refused(valid_config).invalid_fields == [f"execution.{field}"]


class TestIntervalsAndPersistence:
    @pytest.mark.parametrize(
        ("field", "low", "high"),
        [("reconciliation_interval", 60.0, 3600.0), ("reconciliation_balance_tolerance", 0.0, 100.0)],
    )
    def test_the_execution_bounds_load(self, valid_config, field, low, high):
        for value in (low, high, int(high)):
            valid_config["execution"] = {field: value}

            loaded = getattr(load_config(valid_config).execution, field)

            assert loaded == value
            assert type(loaded) is float

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("reconciliation_interval", 0),
            ("reconciliation_interval", 59.9),
            ("reconciliation_interval", 3600.1),
            ("reconciliation_interval", "300"),
            ("reconciliation_interval", True),
            ("reconciliation_interval", None),
            ("reconciliation_interval", float("nan")),
            ("reconciliation_balance_tolerance", -0.01),
            ("reconciliation_balance_tolerance", 100.5),
            ("reconciliation_balance_tolerance", False),
            ("reconciliation_balance_tolerance", float("inf")),
        ],
    )
    def test_an_execution_value_out_of_its_range_is_refused(self, valid_config, field, value):
        valid_config["execution"] = {field: value}

        assert _refused(valid_config).invalid_fields == [f"execution.{field}"]

    def test_a_full_persistence_section_loads(self, valid_config):
        for interval in (1, 1.0, 60, 3600.0):
            valid_config["persistence"] = {
                "enabled": False,
                "db_path": "data/state.db",
                "checkpoint_interval_seconds": interval,
            }

            assert load_config(valid_config).persistence == PersistenceConfig(
                enabled=False, db_path="data/state.db", checkpoint_interval_seconds=float(interval)
            )

    def test_an_empty_persistence_section_loads_the_defaults(self, valid_config):
        valid_config["persistence"] = {}

        assert load_config(valid_config).persistence == PersistenceConfig()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("enabled", "false"),
            ("enabled", 1),
            ("enabled", 0),
            ("enabled", None),
            ("db_path", ""),
            ("db_path", "   "),
            ("db_path", 5),
            ("checkpoint_interval_seconds", 0),
            ("checkpoint_interval_seconds", 0.5),
            ("checkpoint_interval_seconds", 3601),
            ("checkpoint_interval_seconds", True),
            ("checkpoint_interval_seconds", "60"),
            ("checkpoint_interval_seconds", None),
            ("checkpoint_interval_seconds", float("inf")),
        ],
    )
    def test_a_persistence_value_of_the_wrong_kind_is_refused(self, valid_config, field, value):
        valid_config["persistence"] = {field: value}

        assert _refused(valid_config).invalid_fields == [f"persistence.{field}"]

    def test_a_null_db_path_loads_as_the_default_path(self, valid_config):
        valid_config["persistence"] = {"db_path": None}

        assert load_config(valid_config).persistence.db_path is None

    @pytest.mark.parametrize("section", ["persistence", "execution", "exchange"])
    def test_a_section_that_is_not_an_object_is_refused(self, valid_config, section):
        valid_config[section] = "on"

        assert _refused(valid_config).invalid_fields == [section]

    @pytest.mark.parametrize("path", sorted(CONFIG_DIR.glob("*.json")), ids=lambda path: path.name)
    def test_every_shipped_config_loads(self, path):
        assert isinstance(load_config(json.loads(path.read_text())), BotConfig)


class TestPaperTradingNeedsASandbox:
    @pytest.mark.parametrize("name", ["binance", "bybit", "okx"])
    def test_paper_on_an_exchange_with_a_sandbox_loads(self, valid_config, name):
        valid_config["exchange"].update(name=name, trading_mode="paper_trading")

        assert load_config(valid_config).exchange.name == name

    def test_paper_on_an_exchange_without_a_sandbox_is_refused_and_named(self, valid_config, caplog):
        valid_config["exchange"].update(name="kraken", trading_mode="paper_trading")

        with caplog.at_level(logging.ERROR):
            error = _refused(valid_config)

        assert error.invalid_fields == ["exchange.name"]
        [message] = [r.getMessage() for r in caplog.records if "no test environment" in r.getMessage()]
        assert message.startswith("exchange.name: kraken has no test environment in ccxt; paper_trading needs one (")
        listed = message.split("(", 1)[1].rstrip(").").split(", ")
        assert {"binance", "bybit", "okx"} <= set(listed)
        assert "kraken" not in listed

    @pytest.mark.parametrize("mode", ["live", "backtest"])
    def test_an_exchange_without_a_sandbox_loads_outside_paper(self, valid_config, mode):
        valid_config["exchange"].update(name="kraken", trading_mode=mode)

        assert load_config(valid_config).exchange.name == "kraken"

    def test_a_name_ccxt_does_not_know_is_left_to_the_exchange_service(self, valid_config):
        valid_config["exchange"].update(name="notanexchange", trading_mode="paper_trading")

        assert load_config(valid_config).exchange.name == "notanexchange"


def test_an_unknown_key_in_a_nested_optional_section_is_refused_with_its_path(valid_config):
    valid_config["risk_management"]["trailing_stop_los"] = {"enabled": True}

    assert _refused(valid_config).invalid_fields == ["risk_management.trailing_stop_los"]


def test_an_unknown_key_inside_a_section_is_refused_with_its_path(valid_config):
    valid_config["grid_strategy"]["range"]["middle"] = 3000

    assert _refused(valid_config).invalid_fields == ["grid_strategy.range.middle"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("grid_strategy", "num_grids"), "10"),
        (("grid_strategy", "num_grids"), True),
        (("risk_management", "take_profit", "enabled"), 1),
        (("risk_management", "take_profit", "threshold"), True),
        (("exchange", "trading_fee"), "0.001"),
        (("exchange", "trading_fee"), True),
        (("grid_strategy", "buy_ratio"), True),
        (("trading_settings", "initial_balance"), "150"),
        (("grid_strategy", "range", "top"), "3100"),
    ],
)
def test_a_number_or_boolean_of_the_wrong_kind_is_refused_never_coerced(valid_config, path, value):
    _set(valid_config, path, value)

    assert ".".join(path) in _refused(valid_config).invalid_fields


@pytest.mark.parametrize(
    "path",
    [
        ("exchange", "trading_fee"),
        ("trading_settings", "initial_balance"),
        ("grid_strategy", "range", "top"),
        ("risk_management", "stop_loss", "threshold"),
    ],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_number_is_refused(valid_config, path, value):
    _set(valid_config, path, value)

    assert ".".join(path) in _refused(valid_config).invalid_fields


def test_a_zero_initial_balance_is_refused(valid_config):
    valid_config["trading_settings"]["initial_balance"] = 0

    assert _refused(valid_config).invalid_fields == ["trading_settings.initial_balance"]


def test_max_slippage_is_refused_as_a_removed_key(valid_config, caplog):
    valid_config["execution"] = {"max_slippage": 0.01}

    with caplog.at_level(logging.ERROR):
        error = _refused(valid_config)

    assert error.invalid_fields == ["execution.max_slippage"]
    assert "removed" in caplog.text


def test_every_refused_field_is_logged_once(valid_config, caplog):
    valid_config["grid_strategy"]["num_grids"] = 0
    del valid_config["pair"]["base_currency"]

    with caplog.at_level(logging.ERROR):
        _refused(valid_config)

    assert len([r for r in caplog.records if "grid_strategy.num_grids" in r.getMessage()]) == 1
    assert len([r for r in caplog.records if "pair.base_currency" in r.getMessage()]) == 1


def test_explicit_defaults_load_like_omitted_ones(valid_config):
    explicit = copy.deepcopy(valid_config)
    explicit["grid_strategy"]["buy_ratio"] = 1.0
    explicit["grid_strategy"]["dynamic_spacing"] = {"enabled": False}
    explicit["risk_management"]["trailing_stop_loss"] = {"enabled": False, "on_trigger": "stop"}
    explicit["execution"] = {}

    assert load_config(explicit) == load_config(valid_config)


def test_the_loaded_config_is_frozen(valid_config):
    config = load_config(valid_config)

    with pytest.raises(AttributeError):
        config.grid_strategy.num_grids = 5


def test_loading_does_not_change_the_raw_dict(valid_config):
    before = copy.deepcopy(valid_config)

    load_config(valid_config)

    assert valid_config == before


_SCHEMA_VALUES: dict[str, Callable[[BotConfig], Any]] = {
    "get_exchange_name": lambda c: c.exchange.name,
    "get_trading_fee": lambda c: c.exchange.trading_fee,
    "get_maker_fee": lambda c: c.exchange.maker_fee,
    "get_taker_fee": lambda c: c.exchange.taker_fee,
    "get_post_only": lambda c: c.exchange.post_only,
    "get_trading_mode": lambda c: c.exchange.trading_mode.value,
    "get_base_currency": lambda c: c.pair.base_currency,
    "get_quote_currency": lambda c: c.pair.quote_currency,
    "get_timeframe": lambda c: c.trading_settings.timeframe,
    "get_start_date": lambda c: c.trading_settings.period.start_date,
    "get_end_date": lambda c: c.trading_settings.period.end_date,
    "get_initial_balance": lambda c: c.trading_settings.initial_balance,
    "get_historical_data_file": lambda c: c.trading_settings.historical_data_file,
    "get_strategy_type": lambda c: c.grid_strategy.type.value,
    "get_spacing_type": lambda c: c.grid_strategy.spacing.value,
    "get_num_grids": lambda c: c.grid_strategy.num_grids,
    "get_top_range": lambda c: c.grid_strategy.range.top,
    "get_bottom_range": lambda c: c.grid_strategy.range.bottom,
    "get_buy_ratio": lambda c: c.grid_strategy.buy_ratio,
    "get_sell_ratio": lambda c: c.grid_strategy.sell_ratio,
    "is_dynamic_spacing_enabled": lambda c: c.grid_strategy.dynamic_spacing.enabled,
    "get_dynamic_atr_period": lambda c: c.grid_strategy.dynamic_spacing.atr_period,
    "get_atr_spacing_multiplier": lambda c: c.grid_strategy.dynamic_spacing.atr_spacing_multiplier,
    "get_regrid_threshold": lambda c: c.grid_strategy.dynamic_spacing.regrid_threshold,
    "get_cooldown_bars": lambda c: c.grid_strategy.dynamic_spacing.cooldown_bars,
    "is_take_profit_enabled": lambda c: c.risk_management.take_profit.enabled,
    "get_take_profit_threshold": lambda c: c.risk_management.take_profit.threshold,
    "is_stop_loss_enabled": lambda c: c.risk_management.stop_loss.enabled,
    "get_stop_loss_threshold": lambda c: c.risk_management.stop_loss.threshold,
    "is_trailing_stop_loss_enabled": lambda c: c.risk_management.trailing_stop_loss.enabled,
    "get_trailing_atr_period": lambda c: c.risk_management.trailing_stop_loss.atr_period,
    "get_trailing_atr_multiplier": lambda c: c.risk_management.trailing_stop_loss.atr_multiplier,
    "get_trailing_on_trigger": lambda c: c.risk_management.trailing_stop_loss.on_trigger.value,
    "get_max_retries": lambda c: c.execution.max_retries,
    "get_retry_delay": lambda c: c.execution.retry_delay,
    "get_order_polling_interval": lambda c: c.execution.order_polling_interval,
    "get_websocket_max_retries": lambda c: c.execution.websocket_max_retries,
    "get_websocket_retry_base_delay": lambda c: c.execution.websocket_retry_base_delay,
    "get_health_check_interval": lambda c: c.execution.health_check_interval,
    "get_circuit_breaker_failure_threshold": lambda c: c.execution.circuit_breaker_failure_threshold,
    "get_circuit_breaker_recovery_timeout": lambda c: c.execution.circuit_breaker_recovery_timeout,
    "get_circuit_breaker_half_open_max_calls": lambda c: c.execution.circuit_breaker_half_open_max_calls,
    "get_backtest_slippage": lambda c: c.execution.backtest_slippage,
    "get_backtest_min_amount": lambda c: c.execution.backtest_min_amount,
    "get_backtest_min_cost": lambda c: c.execution.backtest_min_cost,
    "get_reconciliation_interval": lambda c: c.execution.reconciliation_interval,
    "get_reconciliation_balance_tolerance": lambda c: c.execution.reconciliation_balance_tolerance,
    "get_logging_level": lambda c: c.logging.log_level,
    "should_log_to_file": lambda c: c.logging.log_to_file,
    "is_persistence_enabled": lambda c: c.persistence.enabled,
    "get_checkpoint_interval_seconds": lambda c: c.persistence.checkpoint_interval_seconds,
}


def _raw_for(name: str, valid_config: dict[str, Any]) -> dict[str, Any]:
    if name == "valid_config":
        return valid_config
    return json.loads((CONFIG_DIR / name).read_text())


@pytest.mark.parametrize("name", ["config.json", "config.live.example.json", "valid_config"])
def test_every_shipped_config_loads_to_the_values_the_old_getters_returned(name, valid_config):
    old = json.loads(OLD_GETTER_VALUES.read_text())[name]
    config = load_config(_raw_for(name, valid_config))

    for getter, value in old.items():
        if getter == "get_max_slippage":
            continue
        expected = value.upper() if getter == "get_logging_level" else value
        assert _SCHEMA_VALUES[getter](config) == expected, getter
