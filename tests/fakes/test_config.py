from grid_trading_bot.config.trading_mode import TradingMode
from tests.fakes.config import config_manager_from, deep_merge


def test_deep_merge_overrides_nested_keys_and_keeps_the_rest():
    base = {"a": {"b": 1, "c": 2}}

    assert deep_merge(base, {"a": {"b": 3}}) == {"a": {"b": 3, "c": 2}}
    assert base == {"a": {"b": 1, "c": 2}}


def test_config_manager_from_applies_the_overrides(valid_config):
    manager = config_manager_from(valid_config, {"exchange": {"trading_mode": "paper_trading"}})

    assert manager.config.exchange.trading_mode is TradingMode.PAPER_TRADING
