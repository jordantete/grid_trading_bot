import logging

from core.domain.spacing_type import SpacingType
from core.domain.strategy_type import StrategyType

from .exceptions import ConfigValidationError
from .trading_mode import TradingMode


class ConfigValidator:
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)

    def validate(self, config):
        missing_fields = []
        invalid_fields = []
        missing_fields += self._validate_required_fields(config)
        invalid_fields += self._validate_exchange(config)
        missing_fields += self._validate_pair(config)
        missing_trading_settings, invalid_trading_settings = self._validate_trading_settings(config)
        missing_fields += missing_trading_settings
        invalid_fields += invalid_trading_settings
        missing_grid_settings, invalid_grid_settings = self._validate_grid_strategy(config)
        missing_fields += missing_grid_settings
        invalid_fields += invalid_grid_settings
        invalid_fields += self._validate_limits(config)
        missing_logging_settings, invalid_logging_settings = self._validate_logging(config)
        missing_fields += missing_logging_settings
        invalid_fields += invalid_logging_settings

        if missing_fields or invalid_fields:
            raise ConfigValidationError(missing_fields=missing_fields, invalid_fields=invalid_fields)

    def _validate_required_fields(self, config):
        required_fields = ["exchange", "pair", "trading_settings", "grid_strategy", "risk_management", "logging"]
        missing_fields = [field for field in required_fields if field not in config]
        if missing_fields:
            self.logger.error(f"Missing required fields: {missing_fields}")
        return missing_fields

    def _validate_exchange(self, config):
        invalid_fields = []
        exchange = config.get("exchange", {})

        if not exchange.get("name"):
            self.logger.error("Exchange name is missing.")
            invalid_fields.append("exchange.name")

        trading_fee = exchange.get("trading_fee")
        if trading_fee is None or not isinstance(trading_fee, float | int) or trading_fee < 0:
            self.logger.error("Invalid or missing trading fee.")
            invalid_fields.append("exchange.trading_fee")

        trading_mode_str = exchange.get("trading_mode")
        if not trading_mode_str:
            invalid_fields.append("exchange.trading_mode")
        else:
            try:
                TradingMode.from_string(trading_mode_str)
            except ValueError:
                invalid_fields.append("exchange.trading_mode")

        return invalid_fields

    def _validate_pair(self, config):
        missing_fields = []
        pair = config.get("pair", {})

        if not pair.get("base_currency"):
            missing_fields.append("pair.base_currency")
        if not pair.get("quote_currency"):
            missing_fields.append("pair.quote_currency")

        if missing_fields:
            self.logger.error(f"Missing pair configuration fields: {missing_fields}")

        return missing_fields

    def _validate_trading_settings(self, config):
        missing_fields = []
        invalid_fields = []
        trading_settings = config.get("trading_settings", {})

        if not trading_settings.get("initial_balance"):
            missing_fields.append("trading_settings.initial_balance")

        # Validate timeframe
        timeframe = trading_settings.get("timeframe")
        valid_timeframes = ["1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "6h", "12h", "1d", "1w", "1M"]
        if timeframe not in valid_timeframes:
            self.logger.error(f"Invalid timeframe: {timeframe}. Must be one of {valid_timeframes}.")
            invalid_fields.append("trading_settings.timeframe")

        # Validate period
        period = trading_settings.get("period", {})
        start_date = period.get("start_date")
        end_date = period.get("end_date")

        if not start_date:
            missing_fields.append("trading_settings.period.start_date")
        if not end_date:
            missing_fields.append("trading_settings.period.end_date")

        return missing_fields, invalid_fields

    def _validate_grid_strategy(self, config):
        missing_fields = []
        invalid_fields = []
        grid = config.get("grid_strategy", {})

        grid_type = grid.get("type")
        if grid_type is None:
            missing_fields.append("grid_strategy.type")
        else:
            try:
                StrategyType.from_string(grid_type)

            except ValueError as e:
                self.logger.error(str(e))
                invalid_fields.append("grid_strategy.type")

        spacing = grid.get("spacing")
        if spacing is None:
            missing_fields.append("grid_strategy.spacing")
        else:
            try:
                SpacingType.from_string(spacing)

            except ValueError as e:
                self.logger.error(str(e))
                invalid_fields.append("grid_strategy.spacing")

        num_grids = grid.get("num_grids")
        if num_grids is None:
            missing_fields.append("grid_strategy.num_grids")
        elif not isinstance(num_grids, int) or num_grids <= 0:
            self.logger.error("Grid strategy 'num_grids' must be a positive integer.")
            invalid_fields.append("grid_strategy.num_grids")

        range_ = grid.get("range", {})
        top = range_.get("top")
        bottom = range_.get("bottom")
        if top is None:
            missing_fields.append("grid_strategy.range.top")
        if bottom is None:
            missing_fields.append("grid_strategy.range.bottom")

        if top is not None and bottom is not None:
            if not isinstance(top, int | float) or not isinstance(bottom, int | float):
                self.logger.error("'top' and 'bottom' in 'grid_strategy.range' must be numbers.")
                invalid_fields.append("grid_strategy.range.top")
                invalid_fields.append("grid_strategy.range.bottom")
            elif bottom >= top:
                self.logger.error("'grid_strategy.range.bottom' must be less than 'grid_strategy.range.top'.")
                invalid_fields.append("grid_strategy.range.top")
                invalid_fields.append("grid_strategy.range.bottom")

        return missing_fields, invalid_fields

    def _validate_limits(self, config):
        invalid_fields = []
        limits = config.get("risk_management", {})
        take_profit = limits.get("take_profit", {})
        stop_loss = limits.get("stop_loss", {})

        # Validate take profit
        if not isinstance(take_profit.get("enabled"), bool):
            self.logger.error("Take profit enabled flag must be a boolean.")
            invalid_fields.append("risk_management.take_profit.enabled")

        if take_profit.get("threshold") is None or not isinstance(take_profit.get("threshold"), float | int):
            self.logger.error("Invalid or missing take profit threshold.")
            invalid_fields.append("risk_management.take_profit.threshold")

        # Validate stop loss
        if not isinstance(stop_loss.get("enabled"), bool):
            self.logger.error("Stop loss enabled flag must be a boolean.")
            invalid_fields.append("risk_management.stop_loss.enabled")

        if stop_loss.get("threshold") is None or not isinstance(stop_loss.get("threshold"), float | int):
            self.logger.error("Invalid or missing stop loss threshold.")
            invalid_fields.append("risk_management.stop_loss.threshold")

        return invalid_fields

    def _validate_logging(self, config):
        missing_fields = []
        invalid_fields = []
        logging_settings = config.get("logging", {})

        # Validate log level
        log_level = logging_settings.get("log_level")
        valid_log_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if log_level is None:
            missing_fields.append("logging.log_level")
        elif log_level.upper() not in valid_log_levels:
            self.logger.error(f"Invalid log level: {log_level}. Must be one of {valid_log_levels}.")
            invalid_fields.append("logging.log_level")

        # Validate log to file
        if not isinstance(logging_settings.get("log_to_file"), bool):
            self.logger.error("log_to_file must be a boolean.")
            invalid_fields.append("logging.log_to_file")

        if missing_fields:
            self.logger.error(f"Missing logging fields: {missing_fields}")

        return missing_fields, invalid_fields
