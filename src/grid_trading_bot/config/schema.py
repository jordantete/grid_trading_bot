import dataclasses
from dataclasses import dataclass, field
from enum import Enum
import functools
import logging
import math
import types
from typing import Any, Union, get_args, get_origin, get_type_hints

import ccxt

from grid_trading_bot.core.domain.spacing_type import SpacingType
from grid_trading_bot.core.domain.strategy_type import StrategyType

from .exceptions import ConfigValidationError
from .trading_mode import TradingMode

logger = logging.getLogger(__name__)

TIMEFRAMES = ("1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "6h", "12h", "1d", "1w", "1M")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_NON_EMPTY = {"non_empty": True}
_NON_NEGATIVE = {"range": (0, None)}
_POSITIVE = {"range": (0, None), "low_exclusive": True}
_RATIO = {"range": (0, 1.0), "low_exclusive": True}
_ATR_PERIOD = {"range": (2, None)}

_REMOVED_KEYS = {"execution.max_slippage"}


class TrailingTrigger(Enum):
    STOP = "stop"
    REGRID = "regrid"


@dataclass(frozen=True)
class ExchangeConfig:
    name: str = field(metadata=_NON_EMPTY)
    trading_fee: float = field(metadata=_NON_NEGATIVE)
    trading_mode: TradingMode
    maker_fee: float = field(metadata={**_NON_NEGATIVE, "fallback": "trading_fee"})
    taker_fee: float = field(metadata={**_NON_NEGATIVE, "fallback": "trading_fee"})
    post_only: bool = False


@dataclass(frozen=True)
class PairConfig:
    base_currency: str = field(metadata=_NON_EMPTY)
    quote_currency: str = field(metadata=_NON_EMPTY)


@dataclass(frozen=True)
class PeriodConfig:
    start_date: str = field(metadata=_NON_EMPTY)
    end_date: str = field(metadata=_NON_EMPTY)


@dataclass(frozen=True)
class TradingSettings:
    initial_balance: float = field(metadata=_POSITIVE)
    timeframe: str = field(metadata={"choices": TIMEFRAMES})
    period: PeriodConfig
    historical_data_file: str | None = None


@dataclass(frozen=True)
class RangeConfig:
    top: float
    bottom: float


@dataclass(frozen=True)
class DynamicSpacingConfig:
    enabled: bool
    atr_period: int = field(default=14, metadata=_ATR_PERIOD)
    atr_spacing_multiplier: float = field(default=1.0, metadata=_POSITIVE)
    regrid_threshold: float = field(default=0.3, metadata=_POSITIVE)
    cooldown_bars: int = field(default=60, metadata=_NON_NEGATIVE)


@dataclass(frozen=True)
class GridStrategyConfig:
    type: StrategyType
    spacing: SpacingType
    num_grids: int = field(metadata={"range": (1, None)})
    range: RangeConfig | None = None
    buy_ratio: float = field(default=1.0, metadata=_RATIO)
    sell_ratio: float = field(default=1.0, metadata=_RATIO)
    # The flag has no field default so that a section written without it is refused.
    dynamic_spacing: DynamicSpacingConfig = field(default_factory=lambda: DynamicSpacingConfig(enabled=False))


@dataclass(frozen=True)
class TakeProfitConfig:
    enabled: bool
    threshold: float


@dataclass(frozen=True)
class StopLossConfig:
    enabled: bool
    threshold: float


@dataclass(frozen=True)
class TrailingStopLossConfig:
    enabled: bool
    atr_period: int = field(default=14, metadata=_ATR_PERIOD)
    atr_multiplier: float = field(default=2.5, metadata=_POSITIVE)
    on_trigger: TrailingTrigger = TrailingTrigger.STOP


@dataclass(frozen=True)
class RiskManagementConfig:
    take_profit: TakeProfitConfig
    stop_loss: StopLossConfig
    trailing_stop_loss: TrailingStopLossConfig = field(default_factory=lambda: TrailingStopLossConfig(enabled=False))


@dataclass(frozen=True)
class ExecutionConfig:
    max_retries: int = field(default=3, metadata={"range": (1, 20)})
    retry_delay: float = field(default=1.0, metadata={"range": (0.1, 60.0)})
    order_polling_interval: float = field(default=15.0, metadata={"range": (1.0, 300.0)})
    websocket_max_retries: int = field(default=5, metadata={"range": (1, 50)})
    websocket_retry_base_delay: int = field(default=5, metadata={"range": (1, 120)})
    health_check_interval: int = field(default=60, metadata={"range": (10, 3600)})
    circuit_breaker_failure_threshold: int = field(default=5, metadata={"range": (1, 50)})
    circuit_breaker_recovery_timeout: float = field(default=60.0, metadata={"range": (1.0, 600.0)})
    circuit_breaker_half_open_max_calls: int = field(default=1, metadata={"range": (1, 10)})
    backtest_slippage: float = field(default=0.0, metadata={"range": (0.0, 0.1)})
    backtest_min_amount: float | None = field(default=None, metadata=_NON_NEGATIVE)
    backtest_min_cost: float | None = field(default=None, metadata=_NON_NEGATIVE)
    reconciliation_interval: float = field(default=300.0, metadata={"range": (60.0, 3600.0)})
    reconciliation_balance_tolerance: float = field(default=0.01, metadata={"range": (0.0, 100.0)})


@dataclass(frozen=True)
class LoggingConfig:
    log_level: str = field(metadata={"choices": LOG_LEVELS, "upper_case": True})
    log_to_file: bool


@dataclass(frozen=True)
class PersistenceConfig:
    enabled: bool = True
    db_path: str | None = field(default=None, metadata=_NON_EMPTY)
    checkpoint_interval_seconds: float = field(default=60.0, metadata={"range": (1.0, 3600.0)})


@dataclass(frozen=True)
class BotConfig:
    exchange: ExchangeConfig
    pair: PairConfig
    trading_settings: TradingSettings
    grid_strategy: GridStrategyConfig
    risk_management: RiskManagementConfig
    logging: LoggingConfig
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    persistence: PersistenceConfig = field(default_factory=PersistenceConfig)


def load_config(raw: dict[str, Any]) -> BotConfig:
    """Validate a raw config dict against the schema and return it as typed, frozen objects with defaults applied.

    Raises ConfigValidationError naming every missing and invalid field at once.
    """
    loader = _Loader()
    values = loader.walk(BotConfig, raw, "")
    loader.check_cross_field_rules(values)
    if loader.missing or loader.invalid:
        raise ConfigValidationError(missing_fields=loader.missing, invalid_fields=loader.invalid)
    return _build(BotConfig, values)


class _Absent:
    pass


_ABSENT = _Absent()


@functools.cache
def _fields_of(cls: type) -> tuple[tuple[dataclasses.Field[Any], Any], ...]:
    hints = get_type_hints(cls)
    return tuple((f, hints[f.name]) for f in dataclasses.fields(cls))


def _unwrap_optional(hint: Any) -> tuple[Any, bool]:
    args = get_args(hint)
    if get_origin(hint) in (Union, types.UnionType) and len(args) == 2 and type(None) in args:
        return next(arg for arg in args if arg is not type(None)), True
    return hint, False


def _is_section(hint: Any) -> bool:
    return isinstance(hint, type) and dataclasses.is_dataclass(hint)


def _default_of(f: dataclasses.Field[Any]) -> Any:
    if f.default_factory is not dataclasses.MISSING:
        value = f.default_factory()
        return dataclasses.asdict(value) if dataclasses.is_dataclass(value) and not isinstance(value, type) else value
    if f.default is not dataclasses.MISSING:
        return f.default
    return _ABSENT


def _has_sandbox(name: str) -> bool:
    return getattr(ccxt, name)().has.get("sandbox") is True


class _Loader:
    def __init__(self) -> None:
        self.missing: list[str] = []
        self.invalid: list[str] = []

    def _missing(self, path: str) -> None:
        logger.error(f"{path} is missing.")
        self.missing.append(path)

    def _invalid(self, path: str, reason: str) -> None:
        logger.error(f"{path} {reason}.")
        self.invalid.append(path)

    def walk(self, cls: type, raw: Any, path: str) -> dict[str, Any]:
        values: dict[str, Any] = {}
        fields = _fields_of(cls)
        known = {f.name for f, _ in fields}
        for key in raw:
            key_path = f"{path}.{key}" if path else key
            if key_path in _REMOVED_KEYS:
                self._invalid(key_path, "was removed and is no longer read; delete it from the config")
            elif key not in known:
                self._invalid(key_path, "is not a known key")

        for f, hint in fields:
            field_path = f"{path}.{f.name}" if path else f.name
            inner, optional = _unwrap_optional(hint)
            if f.name not in raw:
                default = _default_of(f)
                if default is not _ABSENT:
                    values[f.name] = default
                elif _is_section(inner) and path:
                    values[f.name] = self.walk(inner, {}, field_path)
                elif "fallback" not in f.metadata:
                    self._missing(field_path)
                continue

            value = raw[f.name]
            if value is None:
                if optional:
                    values[f.name] = None
                else:
                    self._invalid(field_path, "must not be null")
                continue

            if _is_section(inner):
                if isinstance(value, dict):
                    values[f.name] = self.walk(inner, value, field_path)
                else:
                    self._invalid(field_path, "must be an object")
                continue

            loaded = self._scalar(inner, value, f.metadata, field_path)
            if loaded is not _ABSENT:
                values[f.name] = loaded

        for f, _ in fields:
            source = f.metadata.get("fallback")
            if source and f.name not in raw and source in values:
                values[f.name] = values[source]
        return values

    def _scalar(self, hint: Any, value: Any, metadata: Any, path: str) -> Any:
        if isinstance(hint, type) and issubclass(hint, Enum):
            choices = [member.value for member in hint]
            if isinstance(value, str) and value in choices:
                return hint(value)
            self._invalid(path, f"must be one of {', '.join(choices)}")
            return _ABSENT

        if hint is bool:
            if isinstance(value, bool):
                return value
            self._invalid(path, "must be a boolean")
            return _ABSENT

        if hint is str:
            return self._string(value, metadata, path)

        is_int = hint is int
        allowed = int if is_int else int | float
        number_ok = isinstance(value, allowed) and not isinstance(value, bool) and math.isfinite(value)
        if number_ok and self._in_range(value, metadata):
            return value if is_int else float(value)
        self._invalid(path, f"must be {_describe_number(is_int, metadata)}")
        return _ABSENT

    def _string(self, value: Any, metadata: Any, path: str) -> Any:
        if not isinstance(value, str):
            self._invalid(path, "must be a string")
            return _ABSENT
        if metadata.get("upper_case"):
            value = value.upper()
        if metadata.get("non_empty") and not value.strip():
            self._invalid(path, "must be a non-empty string")
            return _ABSENT
        choices = metadata.get("choices")
        if choices is not None and value not in choices:
            self._invalid(path, f"must be one of {', '.join(choices)}")
            return _ABSENT
        return value

    @staticmethod
    def _in_range(value: float, metadata: Any) -> bool:
        low, high = metadata.get("range", (None, None))
        if low is not None and (value <= low if metadata.get("low_exclusive") else value < low):
            return False
        return high is None or value <= high

    def check_cross_field_rules(self, values: dict[str, Any]) -> None:
        exchange = values.get("exchange", {})
        grid = values.get("grid_strategy", {})
        dynamic_enabled = grid.get("dynamic_spacing", {}).get("enabled") is True

        range_ = grid.get("range")
        if range_ is None and "range" in grid and not dynamic_enabled:
            self._missing("grid_strategy.range.top")
            self._missing("grid_strategy.range.bottom")
        elif range_ is not None and {"top", "bottom"} <= range_.keys() and range_["bottom"] >= range_["top"]:
            self._invalid("grid_strategy.range.top", "must be above grid_strategy.range.bottom")
            self._invalid("grid_strategy.range.bottom", "must be below grid_strategy.range.top")

        if dynamic_enabled and grid.get("spacing") is SpacingType.GEOMETRIC:
            self._invalid("grid_strategy.spacing", "must be arithmetic: dynamic_spacing does not support geometric")

        trailing = values.get("risk_management", {}).get("trailing_stop_loss", {})
        if trailing.get("on_trigger") is TrailingTrigger.REGRID and not dynamic_enabled:
            self._invalid(
                "risk_management.trailing_stop_loss.on_trigger",
                "'regrid' requires grid_strategy.dynamic_spacing.enabled: true",
            )

        if exchange.get("trading_mode") is TradingMode.PAPER_TRADING and "name" in exchange:
            self._check_paper_exchange(exchange["name"])

    def _check_paper_exchange(self, name: str) -> None:
        # A name ccxt does not know keeps the error the exchange service raises when it connects.
        if name not in ccxt.exchanges or _has_sandbox(name):
            return
        with_sandbox = ", ".join(n for n in ccxt.exchanges if _has_sandbox(n))
        logger.error(
            f"exchange.name: {name} has no test environment in ccxt; paper_trading needs one ({with_sandbox})."
        )
        self.invalid.append("exchange.name")


def _describe_number(is_int: bool, metadata: Any) -> str:
    kind = "an integer" if is_int else "a finite number"
    low, high = metadata.get("range", (None, None))
    lower = ">" if metadata.get("low_exclusive") else ">="
    if low is None:
        return kind
    if high is None:
        return f"{kind} {lower} {low}"
    return f"{kind} {lower} {low} and <= {high}"


def _build(cls: type, values: dict[str, Any]) -> Any:
    kwargs = {}
    for f, hint in _fields_of(cls):
        inner, _ = _unwrap_optional(hint)
        value = values[f.name]
        kwargs[f.name] = _build(inner, value) if _is_section(inner) and isinstance(value, dict) else value
    return cls(**kwargs)
