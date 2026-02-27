import json
import logging
import os
from typing import Any

from grid_trading_bot.core.domain.spacing_type import SpacingType
from grid_trading_bot.core.domain.strategy_type import StrategyType

from .config_validator import ConfigValidator
from .exceptions import ConfigFileNotFoundError, ConfigParseError
from .trading_mode import TradingMode


class ConfigManager:
    def __init__(self, config_file: str, config_validator: ConfigValidator) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.config_file = config_file
        self.config_validator = config_validator
        self.config: dict[str, Any] = {}
        self.load_config()

    def load_config(self) -> None:
        if not os.path.exists(self.config_file):
            self.logger.error(f"Config file {self.config_file} does not exist.")
            raise ConfigFileNotFoundError(self.config_file)

        with open(self.config_file) as file:
            try:
                self.config = json.load(file)
                self.config_validator.validate(self.config)
            except json.JSONDecodeError as e:
                self.logger.error(f"Failed to parse config file {self.config_file}: {e}")
                raise ConfigParseError(self.config_file, e) from e

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    # --- General Accessor Methods ---
    def get_exchange(self) -> dict:
        return self.config.get("exchange", {})

    def get_exchange_name(self) -> str | None:
        exchange = self.get_exchange()
        return exchange.get("name", None)

    def get_trading_fee(self) -> float:
        exchange = self.get_exchange()
        return exchange.get("trading_fee", 0)

    def get_trading_mode(self) -> TradingMode | None:
        exchange = self.get_exchange()
        trading_mode = exchange.get("trading_mode", None)

        if trading_mode:
            return TradingMode.from_string(trading_mode)

    def get_pair(self) -> dict:
        return self.config.get("pair", {})

    def get_base_currency(self) -> str | None:
        pair = self.get_pair()
        return pair.get("base_currency", None)

    def get_quote_currency(self) -> str | None:
        pair = self.get_pair()
        return pair.get("quote_currency", None)

    def get_trading_settings(self) -> dict:
        return self.config.get("trading_settings", {})

    def get_timeframe(self) -> str:
        trading_settings = self.get_trading_settings()
        return trading_settings.get("timeframe", "1h")

    def get_period(self) -> dict:
        trading_settings = self.get_trading_settings()
        return trading_settings.get("period", {})

    def get_start_date(self) -> str | None:
        period = self.get_period()
        return period.get("start_date", None)

    def get_end_date(self) -> str | None:
        period = self.get_period()
        return period.get("end_date", None)

    def get_initial_balance(self) -> float:
        trading_settings = self.get_trading_settings()
        return trading_settings.get("initial_balance", 10000)

    def get_historical_data_file(self) -> str | None:
        trading_settings = self.get_trading_settings()
        return trading_settings.get("historical_data_file", None)

    # --- Grid Accessor Methods ---
    def get_grid_settings(self) -> dict:
        return self.config.get("grid_strategy", {})

    def get_strategy_type(self) -> StrategyType | None:
        grid_settings = self.get_grid_settings()
        strategy_type = grid_settings.get("type", None)

        if strategy_type:
            return StrategyType.from_string(strategy_type)

    def get_spacing_type(self) -> SpacingType | None:
        grid_settings = self.get_grid_settings()
        spacing_type = grid_settings.get("spacing", None)

        if spacing_type:
            return SpacingType.from_string(spacing_type)

    def get_num_grids(self) -> int | None:
        grid_settings = self.get_grid_settings()
        return grid_settings.get("num_grids", None)

    def get_grid_range(self) -> dict:
        grid_settings = self.get_grid_settings()
        return grid_settings.get("range", {})

    def get_top_range(self) -> float | None:
        grid_range = self.get_grid_range()
        return grid_range.get("top", None)

    def get_bottom_range(self) -> float | None:
        grid_range = self.get_grid_range()
        return grid_range.get("bottom", None)

    # --- Risk management (Take Profit / Stop Loss) Accessor Methods ---
    def get_risk_management(self) -> dict:
        return self.config.get("risk_management", {})

    def get_take_profit(self) -> dict:
        risk_management = self.get_risk_management()
        return risk_management.get("take_profit", {})

    def is_take_profit_enabled(self) -> bool:
        take_profit = self.get_take_profit()
        return take_profit.get("enabled", False)

    def get_take_profit_threshold(self) -> float | None:
        take_profit = self.get_take_profit()
        return take_profit.get("threshold", None)

    def get_stop_loss(self) -> dict:
        risk_management = self.get_risk_management()
        return risk_management.get("stop_loss", {})

    def is_stop_loss_enabled(self) -> bool:
        stop_loss = self.get_stop_loss()
        return stop_loss.get("enabled", False)

    def get_stop_loss_threshold(self) -> float | None:
        stop_loss = self.get_stop_loss()
        return stop_loss.get("threshold", None)

    # --- Execution Settings Accessor Methods ---
    def get_execution_settings(self) -> dict:
        return self.config.get("execution", {})

    def get_max_retries(self) -> int:
        return self.get_execution_settings().get("max_retries", 3)

    def get_retry_delay(self) -> float:
        return self.get_execution_settings().get("retry_delay", 1.0)

    def get_max_slippage(self) -> float:
        return self.get_execution_settings().get("max_slippage", 0.01)

    def get_order_polling_interval(self) -> float:
        return self.get_execution_settings().get("order_polling_interval", 15.0)

    def get_websocket_max_retries(self) -> int:
        return self.get_execution_settings().get("websocket_max_retries", 5)

    def get_websocket_retry_base_delay(self) -> int:
        return self.get_execution_settings().get("websocket_retry_base_delay", 5)

    def get_health_check_interval(self) -> int:
        return self.get_execution_settings().get("health_check_interval", 60)

    def get_circuit_breaker_failure_threshold(self) -> int:
        return self.get_execution_settings().get("circuit_breaker_failure_threshold", 5)

    def get_circuit_breaker_recovery_timeout(self) -> float:
        return self.get_execution_settings().get("circuit_breaker_recovery_timeout", 60.0)

    def get_circuit_breaker_half_open_max_calls(self) -> int:
        return self.get_execution_settings().get("circuit_breaker_half_open_max_calls", 1)

    def get_backtest_slippage(self) -> float:
        return self.get_execution_settings().get("backtest_slippage", 0.0)

    def get_reconciliation_interval(self) -> float:
        return self.get_execution_settings().get("reconciliation_interval", 300.0)

    def get_reconciliation_balance_tolerance(self) -> float:
        return self.get_execution_settings().get("reconciliation_balance_tolerance", 0.01)

    # --- Logging Accessor Methods ---
    def get_logging(self):
        return self.config.get("logging", {})

    def get_logging_level(self):
        logging = self.get_logging()
        return logging.get("log_level", {})

    def should_log_to_file(self) -> bool:
        logging = self.get_logging()
        return logging.get("log_to_file", False)
