import copy
from typing import Any

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.schema import BotConfig, load_config


def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def config_manager_from(raw: dict[str, Any], overrides: dict[str, Any] | None = None) -> ConfigManager:
    return ConfigManager.from_dict(deep_merge(raw, overrides or {}))


def bot_config_from(raw: dict[str, Any], overrides: dict[str, Any] | None = None) -> BotConfig:
    return load_config(deep_merge(raw, overrides or {}))
