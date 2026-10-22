import copy
import hashlib
import json

import pytest

from grid_trading_bot.config.schema import load_config
from grid_trading_bot.core.persistence.serializers import compute_config_hash


def _hash(raw):
    return compute_config_hash(load_config(raw))


def _changed(valid_config, section, key, value):
    raw = copy.deepcopy(valid_config)
    raw[section][key] = value
    return raw


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("grid_strategy", "type", "hedged_grid"),
        ("grid_strategy", "spacing", "arithmetic"),
        ("grid_strategy", "num_grids", 21),
        ("grid_strategy", "range", {"top": 3200, "bottom": 2850}),
        ("grid_strategy", "range", {"top": 3100, "bottom": 2800}),
        ("grid_strategy", "buy_ratio", 0.5),
        ("grid_strategy", "sell_ratio", 0.5),
        ("grid_strategy", "dynamic_spacing", {"enabled": False, "atr_period": 20}),
        ("pair", "base_currency", "BTC"),
        ("pair", "quote_currency", "USDC"),
        ("exchange", "name", "kraken"),
        ("exchange", "trading_mode", "live"),
        ("trading_settings", "timeframe", "1h"),
    ],
)
def test_a_change_to_a_hashed_field_moves_the_hash(valid_config, section, key, value):
    assert _hash(_changed(valid_config, section, key, value)) != _hash(valid_config)


def test_the_hash_ignores_risk_management_logging_execution_persistence_and_fees(valid_config):
    changed = copy.deepcopy(valid_config)
    changed["risk_management"]["take_profit"] = {"enabled": True, "threshold": 3800}
    changed["risk_management"]["trailing_stop_loss"] = {"enabled": True, "atr_multiplier": 3.0}
    changed["logging"] = {"log_level": "DEBUG", "log_to_file": False}
    changed["execution"] = {"max_retries": 7, "backtest_slippage": 0.001}
    changed["persistence"] = {"enabled": False, "checkpoint_interval_seconds": 30}
    changed["exchange"] |= {"trading_fee": 0.002, "maker_fee": 0.0, "taker_fee": 0.003, "post_only": True}
    changed["trading_settings"]["initial_balance"] = 500

    assert _hash(changed) == _hash(valid_config)


def test_the_hash_is_a_sha256_hex_digest(valid_config):
    digest = _hash(valid_config)

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_the_hash_is_stable_for_the_same_config(valid_config):
    assert _hash(valid_config) == _hash(copy.deepcopy(valid_config))


def test_explicit_defaults_and_omitted_defaults_hash_alike(valid_config):
    explicit = copy.deepcopy(valid_config)
    explicit["grid_strategy"] |= {"buy_ratio": 1.0, "sell_ratio": 1.0, "dynamic_spacing": {"enabled": False}}
    explicit["grid_strategy"]["range"] = {"top": 3100.0, "bottom": 2850.0}
    assert compute_config_hash(load_config(explicit)) == compute_config_hash(load_config(valid_config))


def test_the_canonical_form_is_pinned(valid_config):
    expected = {
        "strategy_type": "simple_grid",
        "spacing": "geometric",
        "num_grids": 20,
        "range": {"bottom": 2850.0, "top": 3100.0},
        "buy_ratio": 1.0,
        "sell_ratio": 1.0,
        "pair": {"base_currency": "ETH", "quote_currency": "USDT"},
        "dynamic_spacing": {
            "enabled": False,
            "atr_period": 14,
            "atr_spacing_multiplier": 1.0,
            "regrid_threshold": 0.3,
            "cooldown_bars": 60,
        },
        "exchange_name": "binance",
        "trading_mode": "backtest",
        "timeframe": "1m",
    }
    canonical = json.dumps(expected, sort_keys=True, separators=(",", ":"))
    assert compute_config_hash(load_config(valid_config)) == hashlib.sha256(canonical.encode()).hexdigest()


def test_a_dynamic_grid_without_a_range_hashes_a_null_range(valid_config):
    raw = copy.deepcopy(valid_config)
    raw["grid_strategy"]["spacing"] = "arithmetic"
    raw["grid_strategy"]["dynamic_spacing"] = {"enabled": True}
    del raw["grid_strategy"]["range"]
    with_range = copy.deepcopy(raw)
    with_range["grid_strategy"]["range"] = {"top": 3100, "bottom": 2850}

    assert _hash(raw) != _hash(with_range)
