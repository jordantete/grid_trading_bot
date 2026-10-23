import json
import logging
import os
from typing import Any

from .exceptions import ConfigFileNotFoundError, ConfigParseError
from .schema import BotConfig, load_config


class ConfigManager:
    def __init__(self, config_file: str) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.config_file = config_file
        self.config: BotConfig = load_config(self._read_file())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ConfigManager":
        instance = cls.__new__(cls)
        instance.logger = logging.getLogger(cls.__name__)
        instance.config_file = "<dict>"
        instance.config = load_config(raw)
        return instance

    def _read_file(self) -> Any:
        if not os.path.exists(self.config_file):
            self.logger.error(f"Config file {self.config_file} does not exist.")
            raise ConfigFileNotFoundError(self.config_file)

        with open(self.config_file) as file:
            try:
                return json.load(file)
            except ValueError as e:  # JSONDecodeError, or an integer longer than Python's 4300-digit limit
                self.logger.error(f"Failed to parse config file {self.config_file}: {e}")
                raise ConfigParseError(self.config_file, e) from e

    def get_config_hash(self) -> str:
        # Imported here: serializers imports balance_tracker, which imports fee_calculator, which imports this module.
        from grid_trading_bot.core.persistence.serializers import compute_config_hash

        return compute_config_hash(self.config)

    def get_state_db_path(self) -> str:
        if self.config.persistence.db_path is not None:
            return self.config.persistence.db_path
        pair = self.config.pair
        return f"data/{pair.base_currency}_{pair.quote_currency}/state_{self.get_config_hash()[:8]}.db"
