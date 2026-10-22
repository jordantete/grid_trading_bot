import json
import logging
import os
from typing import Any

from grid_trading_bot.core.domain.spacing_type import SpacingType
from grid_trading_bot.core.domain.strategy_type import StrategyType

from .exceptions import ConfigFileNotFoundError, ConfigParseError
from .schema import BotConfig, load_config
from .trading_mode import TradingMode


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
            except json.JSONDecodeError as e:
                self.logger.error(f"Failed to parse config file {self.config_file}: {e}")
                raise ConfigParseError(self.config_file, e) from e

    def get_config_hash(self) -> str:
        from grid_trading_bot.core.persistence.serializers import compute_config_hash

        return compute_config_hash(self.config)

    # --- General Accessor Methods ---
    def get_exchange_name(self) -> str:
        return self.config.exchange.name

    def get_trading_fee(self) -> float:
        return self.config.exchange.trading_fee

    def get_maker_fee(self) -> float:
        """Fee charged on liquidity-adding fills, defaulting to the flat `trading_fee`."""
        return self.config.exchange.maker_fee

    def get_taker_fee(self) -> float:
        """Fee charged on liquidity-taking fills, defaulting to the flat `trading_fee`."""
        return self.config.exchange.taker_fee

    def get_post_only(self) -> bool:
        """Whether limit orders must be posted as maker-only (rejected instead of crossing)."""
        return self.config.exchange.post_only

    def get_trading_mode(self) -> TradingMode:
        return self.config.exchange.trading_mode

    def get_base_currency(self) -> str:
        return self.config.pair.base_currency

    def get_quote_currency(self) -> str:
        return self.config.pair.quote_currency

    def get_timeframe(self) -> str:
        return self.config.trading_settings.timeframe

    def get_start_date(self) -> str:
        return self.config.trading_settings.period.start_date

    def get_end_date(self) -> str:
        return self.config.trading_settings.period.end_date

    def get_initial_balance(self) -> float:
        return self.config.trading_settings.initial_balance

    def get_historical_data_file(self) -> str | None:
        return self.config.trading_settings.historical_data_file

    # --- Grid Accessor Methods ---
    def get_strategy_type(self) -> StrategyType:
        return self.config.grid_strategy.type

    def get_spacing_type(self) -> SpacingType:
        return self.config.grid_strategy.spacing

    def get_num_grids(self) -> int:
        return self.config.grid_strategy.num_grids

    def get_top_range(self) -> float | None:
        grid_range = self.config.grid_strategy.range
        return None if grid_range is None else grid_range.top

    def get_bottom_range(self) -> float | None:
        grid_range = self.config.grid_strategy.range
        return None if grid_range is None else grid_range.bottom

    def get_buy_ratio(self) -> float:
        return self.config.grid_strategy.buy_ratio

    def get_sell_ratio(self) -> float:
        return self.config.grid_strategy.sell_ratio

    def is_dynamic_spacing_enabled(self) -> bool:
        return self.config.grid_strategy.dynamic_spacing.enabled

    def get_dynamic_atr_period(self) -> int:
        return self.config.grid_strategy.dynamic_spacing.atr_period

    def get_atr_spacing_multiplier(self) -> float:
        return self.config.grid_strategy.dynamic_spacing.atr_spacing_multiplier

    def get_regrid_threshold(self) -> float:
        return self.config.grid_strategy.dynamic_spacing.regrid_threshold

    def get_cooldown_bars(self) -> int:
        return self.config.grid_strategy.dynamic_spacing.cooldown_bars

    # --- Risk management (Take Profit / Stop Loss) Accessor Methods ---
    def is_take_profit_enabled(self) -> bool:
        return self.config.risk_management.take_profit.enabled

    def get_take_profit_threshold(self) -> float:
        return self.config.risk_management.take_profit.threshold

    def is_stop_loss_enabled(self) -> bool:
        return self.config.risk_management.stop_loss.enabled

    def get_stop_loss_threshold(self) -> float:
        return self.config.risk_management.stop_loss.threshold

    def is_trailing_stop_loss_enabled(self) -> bool:
        return self.config.risk_management.trailing_stop_loss.enabled

    def get_trailing_atr_period(self) -> int:
        return self.config.risk_management.trailing_stop_loss.atr_period

    def get_trailing_atr_multiplier(self) -> float:
        return self.config.risk_management.trailing_stop_loss.atr_multiplier

    def get_trailing_on_trigger(self) -> str:
        return self.config.risk_management.trailing_stop_loss.on_trigger.value

    # --- Execution Settings Accessor Methods ---
    def get_max_retries(self) -> int:
        return self.config.execution.max_retries

    def get_retry_delay(self) -> float:
        return self.config.execution.retry_delay

    def get_order_polling_interval(self) -> float:
        return self.config.execution.order_polling_interval

    def get_websocket_max_retries(self) -> int:
        return self.config.execution.websocket_max_retries

    def get_websocket_retry_base_delay(self) -> int:
        return self.config.execution.websocket_retry_base_delay

    def get_health_check_interval(self) -> int:
        return self.config.execution.health_check_interval

    def get_circuit_breaker_failure_threshold(self) -> int:
        return self.config.execution.circuit_breaker_failure_threshold

    def get_circuit_breaker_recovery_timeout(self) -> float:
        return self.config.execution.circuit_breaker_recovery_timeout

    def get_circuit_breaker_half_open_max_calls(self) -> int:
        return self.config.execution.circuit_breaker_half_open_max_calls

    def get_backtest_slippage(self) -> float:
        return self.config.execution.backtest_slippage

    def get_backtest_min_amount(self) -> float | None:
        return self.config.execution.backtest_min_amount

    def get_backtest_min_cost(self) -> float | None:
        return self.config.execution.backtest_min_cost

    def get_reconciliation_interval(self) -> float:
        return self.config.execution.reconciliation_interval

    def get_reconciliation_balance_tolerance(self) -> float:
        return self.config.execution.reconciliation_balance_tolerance

    # --- Logging Accessor Methods ---
    def get_logging_level(self) -> str:
        return self.config.logging.log_level

    def should_log_to_file(self) -> bool:
        return self.config.logging.log_to_file

    # --- Persistence Accessor Methods ---
    def is_persistence_enabled(self) -> bool:
        return self.config.persistence.enabled

    def get_state_db_path(self) -> str:
        if self.config.persistence.db_path is not None:
            return self.config.persistence.db_path
        base = self.get_base_currency() or "BASE"
        quote = self.get_quote_currency() or "QUOTE"
        config_hash_short = self.get_config_hash()[:8]
        return f"data/{base}_{quote}/state_{config_hash_short}.db"

    def get_checkpoint_interval_seconds(self) -> float:
        return self.config.persistence.checkpoint_interval_seconds
