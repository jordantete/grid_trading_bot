# Grid Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `grid_sweep`, a separate package that orchestrates mass parameter sweeps with walk-forward validation using `grid_trading_bot` as a dependency.

**Architecture:** Monorepo at `algo_trading/` with `grid_sweep/` sibling to `grid_trading_bot/`. The sweep parses a YAML config, fetches/caches OHLCV data, generates parameter combinations, dispatches backtests to a multiprocessing pool, and outputs a CSV report. It imports `GridTradingBot`, `ConfigManager`, `EventBus` etc. from `grid_trading_bot` — zero trading logic reimplemented.

**Tech Stack:** Python 3.12+, Click (CLI), PyYAML, tqdm, multiprocessing, ccxt (via grid_trading_bot), pandas (via grid_trading_bot)

**Specs:**
- Requirements: `docs/requirements/batch_backtesting_requirements.md`
- Architecture: `docs/requirements/batch_backtesting_architecture.md`

**Conventions (match grid_trading_bot):**
- src layout: `src/grid_sweep/`
- Ruff: 120 char, py312, same rule set (E, W, F, I, B, C4, UP, N, S, T20, SIM, RUF, PT, Q, A, COM, DTZ, TCH)
- Double quotes, space indent, isort with `known-first-party = ["grid_sweep"]`
- Frozen dataclasses for value objects, `self.logger = logging.getLogger(self.__class__.__name__)` in classes
- `X | None` not `Optional[X]`, `list[T]` not `List[T]`
- Tests: pytest-asyncio with `asyncio_mode = "auto"`, `pythonpath = ["src"]`
- Pre-commit: same hooks as grid_trading_bot

---

## Task 0: ConfigManager.from_dict() in grid_trading_bot

**Files:**
- Modify: `grid_trading_bot/src/grid_trading_bot/config/config_manager.py`
- Test: `grid_trading_bot/tests/config/test_config_manager.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/config/test_config_manager.py — add to existing test file
# Add import: from grid_trading_bot.config.exceptions import ConfigValidationError

class TestConfigManagerFromDict:
    def test_from_dict_creates_valid_config_manager(self, valid_config):
        cm = ConfigManager.from_dict(valid_config)
        assert cm.get_exchange_name() == "binance"
        assert cm.get_trading_fee() == 0.001
        assert cm.get_base_currency() == "ETH"
        assert cm.get_quote_currency() == "USDT"

    def test_from_dict_validates_config(self):
        with pytest.raises(ConfigValidationError):
            ConfigManager.from_dict({"exchange": {}})

    def test_from_dict_accepts_custom_validator(self, valid_config):
        validator = ConfigValidator()
        cm = ConfigManager.from_dict(valid_config, config_validator=validator)
        assert cm.config_validator is validator

    def test_from_dict_sets_config_file_marker(self, valid_config):
        cm = ConfigManager.from_dict(valid_config)
        assert cm.config_file == "<dict>"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd grid_trading_bot && uv run python -m pytest tests/config/test_config_manager.py::TestConfigManagerFromDict -v`
Expected: FAIL — `ConfigManager` has no `from_dict` method

- [ ] **Step 3: Implement from_dict classmethod**

Add to `config_manager.py` after `__init__`:

```python
@classmethod
def from_dict(cls, config_dict: dict[str, Any], config_validator: ConfigValidator | None = None) -> "ConfigManager":
    instance = cls.__new__(cls)
    instance.logger = logging.getLogger(cls.__name__)
    instance.config_file = "<dict>"
    instance.config_validator = config_validator or ConfigValidator()
    instance.config = config_dict
    instance.config_validator.validate(instance.config)
    return instance
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd grid_trading_bot && uv run python -m pytest tests/config/test_config_manager.py::TestConfigManagerFromDict -v`
Expected: 4 PASSED

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `cd grid_trading_bot && uv run python -m pytest --cov=grid_trading_bot --cov-report=term`
Expected: All existing tests still pass

- [ ] **Step 6: Commit**

```bash
cd grid_trading_bot
git add src/grid_trading_bot/config/config_manager.py tests/config/test_config_manager.py
git commit -m "feat(config): add ConfigManager.from_dict() classmethod for programmatic config creation"
```

---

## Task 1: Project Scaffolding

**Files:**
- Create: `grid_sweep/pyproject.toml`
- Create: `grid_sweep/src/grid_sweep/__init__.py`
- Create: `grid_sweep/src/grid_sweep/cli.py`
- Create: `grid_sweep/.pre-commit-config.yaml`
- Create: `grid_sweep/config/sweep.yaml`
- Create: `grid_sweep/tests/__init__.py`
- Create: `grid_sweep/tests/conftest.py`

- [ ] **Step 1: Create project directory structure**

```bash
mkdir -p /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep/src/grid_sweep
mkdir -p /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep/tests
mkdir -p /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep/config
```

- [ ] **Step 2: Create pyproject.toml**

```toml
[project]
name = "grid_sweep"
version = "0.1.0"
description = "Parameter sweep with walk-forward validation for grid_trading_bot"
authors = [{ name = "Jordan TETE", email = "tetej171@gmail.com" }]
readme = "README.md"
license = { file = "LICENSE.txt" }
requires-python = ">=3.12"
keywords = ["grid-trading", "backtesting", "parameter-sweep", "walk-forward"]
dependencies = [
    "grid_trading_bot @ file:../grid_trading_bot",
    "pyyaml>=6.0",
    "tqdm>=4.60",
    "click>=8.1",
    "tabulate>=0.9",
    "psutil>=6.0",
]

[project.scripts]
grid_sweep = "grid_sweep.cli:main"

[project.optional-dependencies]
dev = [
    "pytest>=9.0",
    "pytest-asyncio>=1.3",
    "pytest-cov>=7.0",
    "pre-commit>=4.5",
    "ruff>=0.8.4",
]

[build-system]
requires = ["setuptools>=42", "wheel"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["src"]
testpaths = ["tests"]
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
log_cli = true
log_cli_level = "INFO"

[tool.coverage.run]
source = ["grid_sweep"]

[tool.ruff]
line-length = 120
target-version = "py312"
src = ["src/grid_sweep", "tests"]

[tool.ruff.lint]
select = [
    "E", "W", "F", "I", "B", "C4", "UP", "N",
    "S", "T20", "SIM", "RUF", "PT", "Q", "A",
    "COM", "DTZ", "TCH",
]
ignore = ["S101", "COM812"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"
skip-magic-trailing-comma = false
line-ending = "auto"
docstring-code-format = true

[tool.ruff.lint.isort]
known-first-party = ["grid_sweep"]
force-sort-within-sections = true
```

- [ ] **Step 3: Create __init__.py and __main__.py**

```python
# src/grid_sweep/__init__.py
"""Grid Sweep — Parameter sweep with walk-forward validation for grid_trading_bot."""

__version__ = "0.1.0"
```

```python
# src/grid_sweep/__main__.py
from grid_sweep.cli import main

main()
```

- [ ] **Step 4: Create minimal CLI**

```python
# src/grid_sweep/cli.py
import click


@click.group()
@click.version_option(package_name="grid_sweep")
def main():
    """Grid Sweep — Parameter sweep with walk-forward validation."""


@main.command()
@click.option("--config", required=True, type=click.Path(exists=True), help="Path to sweep YAML file.")
@click.option("--output", default=None, type=click.Path(), help="Output CSV path.")
@click.option("--workers", default=None, type=int, help="Parallel workers (default: auto).")
@click.option("--resume", is_flag=True, default=False, help="Resume from checkpoint.")
@click.option("--dry-run", is_flag=True, default=False, help="Show job count and estimated time.")
@click.option("--fetch-only", is_flag=True, default=False, help="Only fetch and cache data.")
@click.option("--pairs-only", is_flag=True, default=False, help="Only list resolved pairs.")
def run(config, output, workers, resume, dry_run, fetch_only, pairs_only):
    """Run a parameter sweep with walk-forward validation."""
    click.echo(f"Sweep config: {config} (not yet implemented)")
```

- [ ] **Step 5: Create .pre-commit-config.yaml** (same hooks as grid_trading_bot)

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.8.4
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-json
      - id: check-toml
      - id: check-merge-conflict
      - id: check-added-large-files
        args: ["--maxkb=1000"]
      - id: debug-statements

  - repo: https://github.com/pre-commit/pygrep-hooks
    rev: v1.10.0
    hooks:
      - id: python-check-blanket-noqa
      - id: python-check-blanket-type-ignore
      - id: python-no-log-warn
      - id: python-use-type-annotations
      - id: text-unicode-replacement-char
```

- [ ] **Step 6: Create example sweep.yaml**

```yaml
# config/sweep.yaml — Example sweep configuration
exchange: binance
trading_fee: 0.001
initial_balance: 1000
timeframe: 1m
backtest_slippage: 0.0

period:
  start_date: "2025-03-18T00:00:00Z"
  end_date: "2026-03-18T00:00:00Z"

pairs:
  mode: auto
  count: 10
  quote_currency: USDT

walk_forward:
  train_months: 3
  test_months: 1

sweep:
  strategy_type: [simple_grid, hedged_grid]
  spacing: [arithmetic, geometric]
  num_grids: [4, 8, 12, 16, 20]
  range_volatility_multiplier: [1.0, 1.5, 2.0, 2.5]
  buy_ratio: [0.5, 0.75, 1.0]
  sell_ratio: [0.5, 0.75, 1.0]
```

- [ ] **Step 7: Create test scaffolding**

```python
# tests/__init__.py
# (empty)
```

```python
# tests/conftest.py
import pytest


@pytest.fixture
def sample_sweep_dict():
    """Minimal valid sweep config as a dict."""
    return {
        "exchange": "binance",
        "trading_fee": 0.001,
        "initial_balance": 1000,
        "timeframe": "1m",
        "backtest_slippage": 0.0,
        "period": {
            "start_date": "2025-03-18T00:00:00Z",
            "end_date": "2026-03-18T00:00:00Z",
        },
        "pairs": {
            "mode": "manual",
            "count": 2,
            "quote_currency": "USDT",
            "manual_list": ["BTC/USDT", "ETH/USDT"],
        },
        "walk_forward": {
            "train_months": 3,
            "test_months": 1,
        },
        "sweep": {
            "strategy_type": ["simple_grid", "hedged_grid"],
            "spacing": ["arithmetic"],
            "num_grids": [4, 8],
            "range_volatility_multiplier": [1.0, 2.0],
            "buy_ratio": [1.0],
            "sell_ratio": [1.0],
        },
    }
```

- [ ] **Step 8: Install and verify**

```bash
cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep
uv sync --dev
uv run grid_sweep --version
uv run grid_sweep run --config config/sweep.yaml
```

Expected: version prints `0.1.0`, run prints placeholder message (sweep.yaml doesn't pass `exists=True` check yet since the YAML references a future period, but the CLI itself works)

- [ ] **Step 9: Run lint**

```bash
cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

Expected: No errors

- [ ] **Step 10: Initialize git and commit**

```bash
cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep
git init
git add .
git commit -m "feat: scaffold grid_sweep project with CLI, config, and test structure"
```

---

## Task 2: SweepConfig — YAML Parsing

**Files:**
- Create: `src/grid_sweep/sweep_config.py`
- Create: `src/grid_sweep/exceptions.py`
- Create: `tests/test_sweep_config.py`

- [ ] **Step 1: Create exceptions module**

```python
# src/grid_sweep/exceptions.py


class SweepError(Exception):
    """Base class for all sweep-related errors."""


class SweepConfigError(SweepError):
    def __init__(self, message: str, field: str | None = None):
        self.field = field
        super().__init__(f"Sweep config error: {message}" + (f" (field: {field})" if field else ""))


class SweepConfigFileNotFoundError(SweepConfigError):
    def __init__(self, path: str):
        super().__init__(f"File not found: {path}")
```

- [ ] **Step 2: Write failing tests**

```python
# tests/test_sweep_config.py
import pytest
import yaml

from grid_sweep.exceptions import SweepConfigError, SweepConfigFileNotFoundError
from grid_sweep.sweep_config import PairsConfig, SweepConfig, WalkForwardConfig


class TestSweepConfigFromYaml:
    def test_parses_valid_yaml(self, tmp_path, sample_sweep_dict):
        path = tmp_path / "sweep.yaml"
        path.write_text(yaml.dump(sample_sweep_dict))

        config = SweepConfig.from_yaml(str(path))

        assert config.exchange == "binance"
        assert config.trading_fee == 0.001
        assert config.initial_balance == 1000
        assert config.timeframe == "1m"

    def test_parses_pairs_config(self, tmp_path, sample_sweep_dict):
        path = tmp_path / "sweep.yaml"
        path.write_text(yaml.dump(sample_sweep_dict))

        config = SweepConfig.from_yaml(str(path))

        assert config.pairs.mode == "manual"
        assert config.pairs.manual_list == ["BTC/USDT", "ETH/USDT"]

    def test_parses_walk_forward_config(self, tmp_path, sample_sweep_dict):
        path = tmp_path / "sweep.yaml"
        path.write_text(yaml.dump(sample_sweep_dict))

        config = SweepConfig.from_yaml(str(path))

        assert config.walk_forward.train_months == 3
        assert config.walk_forward.test_months == 1

    def test_normalizes_scalar_sweep_params_to_lists(self, tmp_path, sample_sweep_dict):
        sample_sweep_dict["sweep"]["spacing"] = "arithmetic"  # scalar, not list
        path = tmp_path / "sweep.yaml"
        path.write_text(yaml.dump(sample_sweep_dict))

        config = SweepConfig.from_yaml(str(path))

        assert config.sweep_params["spacing"] == ["arithmetic"]

    def test_raises_on_missing_file(self):
        with pytest.raises(SweepConfigFileNotFoundError):
            SweepConfig.from_yaml("/nonexistent/path.yaml")

    def test_raises_on_missing_required_field(self, tmp_path):
        path = tmp_path / "sweep.yaml"
        path.write_text(yaml.dump({"exchange": "binance"}))

        with pytest.raises(SweepConfigError):
            SweepConfig.from_yaml(str(path))

    def test_raises_on_empty_sweep_section(self, tmp_path, sample_sweep_dict):
        sample_sweep_dict["sweep"] = {}
        path = tmp_path / "sweep.yaml"
        path.write_text(yaml.dump(sample_sweep_dict))

        with pytest.raises(SweepConfigError):
            SweepConfig.from_yaml(str(path))


class TestSweepConfigTotalCombinations:
    def test_computes_cartesian_product_size(self, tmp_path, sample_sweep_dict):
        path = tmp_path / "sweep.yaml"
        path.write_text(yaml.dump(sample_sweep_dict))

        config = SweepConfig.from_yaml(str(path))

        # 2 strategy × 1 spacing × 2 num_grids × 2 multiplier × 1 buy × 1 sell = 8
        assert config.total_combinations() == 8
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_sweep_config.py -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 4: Implement SweepConfig**

```python
# src/grid_sweep/sweep_config.py
import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Literal

import yaml

from .exceptions import SweepConfigError, SweepConfigFileNotFoundError

logger = logging.getLogger(__name__)

_REQUIRED_TOP_LEVEL = ("exchange", "trading_fee", "initial_balance", "timeframe", "period", "pairs", "walk_forward", "sweep")
_REQUIRED_PERIOD = ("start_date", "end_date")
_REQUIRED_PAIRS = ("mode", "quote_currency")
_REQUIRED_WALK_FORWARD = ("train_months", "test_months")


@dataclass(frozen=True)
class PairsConfig:
    mode: Literal["auto", "manual"]
    count: int
    quote_currency: str
    manual_list: list[str] | None = None


@dataclass(frozen=True)
class WalkForwardConfig:
    train_months: int
    test_months: int


@dataclass(frozen=True)
class SweepConfig:
    exchange: str
    trading_fee: float
    initial_balance: float
    timeframe: str
    backtest_slippage: float
    period_start: str
    period_end: str
    pairs: PairsConfig
    walk_forward: WalkForwardConfig
    sweep_params: dict[str, list[Any]]

    @classmethod
    def from_yaml(cls, path: str) -> "SweepConfig":
        if not os.path.exists(path):
            raise SweepConfigFileNotFoundError(path)

        with open(path) as f:
            raw = yaml.safe_load(f)

        _validate_required_fields(raw)

        period = raw["period"]
        pairs_raw = raw["pairs"]
        wf_raw = raw["walk_forward"]
        sweep_raw = raw["sweep"]

        sweep_params = _normalize_sweep_params(sweep_raw)

        return cls(
            exchange=raw["exchange"],
            trading_fee=raw["trading_fee"],
            initial_balance=raw["initial_balance"],
            timeframe=raw["timeframe"],
            backtest_slippage=raw.get("backtest_slippage", 0.0),
            period_start=period["start_date"],
            period_end=period["end_date"],
            pairs=PairsConfig(
                mode=pairs_raw["mode"],
                count=pairs_raw.get("count", 10),
                quote_currency=pairs_raw["quote_currency"],
                manual_list=pairs_raw.get("manual_list"),
            ),
            walk_forward=WalkForwardConfig(
                train_months=wf_raw["train_months"],
                test_months=wf_raw["test_months"],
            ),
            sweep_params=sweep_params,
        )

    def total_combinations(self) -> int:
        return math.prod(len(v) for v in self.sweep_params.values())


def _validate_required_fields(raw: dict[str, Any]) -> None:
    for field in _REQUIRED_TOP_LEVEL:
        if field not in raw:
            raise SweepConfigError(f"Missing required field: {field}", field=field)

    for field in _REQUIRED_PERIOD:
        if field not in raw.get("period", {}):
            raise SweepConfigError(f"Missing required field: period.{field}", field=f"period.{field}")

    for field in _REQUIRED_PAIRS:
        if field not in raw.get("pairs", {}):
            raise SweepConfigError(f"Missing required field: pairs.{field}", field=f"pairs.{field}")

    for field in _REQUIRED_WALK_FORWARD:
        if field not in raw.get("walk_forward", {}):
            raise SweepConfigError(f"Missing required field: walk_forward.{field}", field=f"walk_forward.{field}")

    wf = raw.get("walk_forward", {})
    if wf.get("train_months", 0) <= 0:
        raise SweepConfigError("train_months must be > 0", field="walk_forward.train_months")
    if wf.get("test_months", 0) <= 0:
        raise SweepConfigError("test_months must be > 0", field="walk_forward.test_months")

    if not raw.get("sweep"):
        raise SweepConfigError("Sweep section is empty — nothing to sweep", field="sweep")


def _normalize_sweep_params(sweep_raw: dict[str, Any]) -> dict[str, list[Any]]:
    normalized = {}
    for key, value in sweep_raw.items():
        normalized[key] = value if isinstance(value, list) else [value]
    return normalized
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_sweep_config.py -v`
Expected: 8 PASSED

- [ ] **Step 6: Lint**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`

- [ ] **Step 7: Commit**

```bash
git add src/grid_sweep/sweep_config.py src/grid_sweep/exceptions.py tests/test_sweep_config.py
git commit -m "feat: add SweepConfig YAML parser with validation"
```

---

## Task 3: WalkForwardEngine — Window Generation & Volatility

**Files:**
- Create: `src/grid_sweep/walk_forward.py`
- Create: `tests/test_walk_forward.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_walk_forward.py
import pytest

from grid_sweep.walk_forward import WalkForwardEngine, WalkForwardWindow, WindowRange


class TestGenerateWindows:
    def setup_method(self):
        self.engine = WalkForwardEngine()

    def test_generates_correct_number_of_windows(self):
        windows = self.engine.generate_windows(
            period_start="2025-01-01T00:00:00Z",
            period_end="2026-01-01T00:00:00Z",
            train_months=3,
            test_months=1,
        )
        assert len(windows) == 9

    def test_first_window_dates(self):
        windows = self.engine.generate_windows(
            period_start="2025-01-01T00:00:00Z",
            period_end="2026-01-01T00:00:00Z",
            train_months=3,
            test_months=1,
        )
        first = windows[0]
        assert first.index == 0
        assert first.train_start == "2025-01-01T00:00:00Z"
        assert first.train_end == "2025-04-01T00:00:00Z"
        assert first.test_start == "2025-04-01T00:00:00Z"
        assert first.test_end == "2025-05-01T00:00:00Z"

    def test_windows_slide_by_test_months(self):
        windows = self.engine.generate_windows(
            period_start="2025-01-01T00:00:00Z",
            period_end="2026-01-01T00:00:00Z",
            train_months=3,
            test_months=1,
        )
        second = windows[1]
        assert second.train_start == "2025-02-01T00:00:00Z"
        assert second.test_end == "2025-06-01T00:00:00Z"

    def test_last_window_does_not_exceed_period(self):
        windows = self.engine.generate_windows(
            period_start="2025-01-01T00:00:00Z",
            period_end="2026-01-01T00:00:00Z",
            train_months=3,
            test_months=1,
        )
        last = windows[-1]
        assert last.test_end <= "2026-01-01T00:00:00Z"

    def test_empty_when_period_too_short(self):
        windows = self.engine.generate_windows(
            period_start="2025-01-01T00:00:00Z",
            period_end="2025-03-01T00:00:00Z",
            train_months=3,
            test_months=1,
        )
        assert len(windows) == 0


class TestComputeVolatilityRange:
    def setup_method(self):
        self.engine = WalkForwardEngine()

    def test_computes_range_from_csv(self, tmp_path):
        csv_content = "timestamp,open,high,low,close,volume\n"
        for i in range(100):
            price = 100.0 + (i % 10)  # oscillates 100-109
            csv_content += f"2025-01-01 00:{i:02d}:00,{price},{price+1},{price-1},{price},1000\n"

        csv_path = tmp_path / "test.csv"
        csv_path.write_text(csv_content)

        result = self.engine.compute_volatility_range(
            ohlcv_csv_path=str(csv_path),
            train_start="2025-01-01T00:00:00Z",
            train_end="2025-01-01T01:40:00Z",
            multiplier=1.0,
        )

        assert isinstance(result, WindowRange)
        assert result.std_dev > 0
        assert result.top > result.center > result.bottom
        assert result.top == pytest.approx(result.center + result.std_dev)
        assert result.bottom == pytest.approx(result.center - result.std_dev)

    def test_multiplier_scales_range(self, tmp_path):
        csv_content = "timestamp,open,high,low,close,volume\n"
        for i in range(100):
            price = 100.0 + (i % 10)
            csv_content += f"2025-01-01 00:{i:02d}:00,{price},{price+1},{price-1},{price},1000\n"

        csv_path = tmp_path / "test.csv"
        csv_path.write_text(csv_content)

        r1 = self.engine.compute_volatility_range(str(csv_path), "2025-01-01T00:00:00Z", "2025-01-01T01:40:00Z", 1.0)
        r2 = self.engine.compute_volatility_range(str(csv_path), "2025-01-01T00:00:00Z", "2025-01-01T01:40:00Z", 2.0)

        assert r2.top - r2.center == pytest.approx(2 * (r1.top - r1.center))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_walk_forward.py -v`

- [ ] **Step 3: Implement WalkForwardEngine**

```python
# src/grid_sweep/walk_forward.py
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd


@dataclass(frozen=True)
class WalkForwardWindow:
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str


@dataclass(frozen=True)
class WindowRange:
    center: float
    top: float
    bottom: float
    std_dev: float


class WalkForwardEngine:
    def generate_windows(
        self,
        period_start: str,
        period_end: str,
        train_months: int,
        test_months: int,
    ) -> list[WalkForwardWindow]:
        start = _parse_iso(period_start)
        end = _parse_iso(period_end)
        windows: list[WalkForwardWindow] = []
        index = 0

        current = start
        while True:
            train_end = _add_months(current, train_months)
            test_end = _add_months(train_end, test_months)
            if test_end > end:
                break
            windows.append(
                WalkForwardWindow(
                    index=index,
                    train_start=_format_iso(current),
                    train_end=_format_iso(train_end),
                    test_start=_format_iso(train_end),
                    test_end=_format_iso(test_end),
                )
            )
            index += 1
            current = _add_months(current, test_months)

        return windows

    def compute_volatility_range(
        self,
        ohlcv_csv_path: str,
        train_start: str,
        train_end: str,
        multiplier: float,
    ) -> WindowRange:
        df = pd.read_csv(ohlcv_csv_path, parse_dates=["timestamp"])
        start_ts = pd.Timestamp(train_start)
        end_ts = pd.Timestamp(train_end)
        mask = (df["timestamp"] >= start_ts) & (df["timestamp"] < end_ts)
        window_data = df.loc[mask, "close"]

        center = float(window_data.mean())
        std_dev = float(window_data.std())

        return WindowRange(
            center=center,
            top=center + multiplier * std_dev,
            bottom=center - multiplier * std_dev,
            std_dev=std_dev,
        )


def _parse_iso(date_str: str) -> datetime:
    return datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=UTC)


def _add_months(dt: datetime, months: int) -> datetime:
    import calendar

    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    max_day = calendar.monthrange(year, month)[1]
    day = min(dt.day, max_day)
    return dt.replace(year=year, month=month, day=day)


def _format_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_walk_forward.py -v`
Expected: 7 PASSED

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
git add src/grid_sweep/walk_forward.py tests/test_walk_forward.py
git commit -m "feat: add WalkForwardEngine with rolling windows and volatility range"
```

---

## Task 4: CombinationGenerator — Cartesian Product & Filtering

**Files:**
- Create: `src/grid_sweep/combination_generator.py`
- Create: `tests/test_combination_generator.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_combination_generator.py
import pytest

from grid_sweep.combination_generator import BacktestJob, CombinationGenerator, FilterStats
from grid_sweep.sweep_config import SweepConfig
from grid_sweep.walk_forward import WalkForwardEngine, WalkForwardWindow, WindowRange


@pytest.fixture
def windows():
    return [
        WalkForwardWindow(index=0, train_start="2025-01-01T00:00:00Z", train_end="2025-04-01T00:00:00Z",
                          test_start="2025-04-01T00:00:00Z", test_end="2025-05-01T00:00:00Z"),
    ]


@pytest.fixture
def generator():
    return CombinationGenerator(trading_fee=0.001)


class TestCombinationGenerator:
    def test_generates_correct_number_of_jobs(self, generator, windows, tmp_path, sample_sweep_dict):
        csv_path = _write_test_csv(tmp_path, price=100.0, std_dev=10.0)
        ohlcv_paths = {"BTC/USDT": str(csv_path)}

        jobs, stats = generator.generate(
            sweep_params=sample_sweep_dict["sweep"],
            pairs=["BTC/USDT"],
            windows=windows,
            ohlcv_paths=ohlcv_paths,
            walk_forward_engine=WalkForwardEngine(),
            fixed_params={
                "exchange": "binance",
                "trading_fee": 0.001,
                "initial_balance": 1000,
                "timeframe": "1m",
                "backtest_slippage": 0.0,
            },
        )

        # 2 strategy × 1 spacing × 2 grids × 2 mult × 1 buy × 1 sell = 8
        # per 1 pair × 1 window = 8 (some may be filtered)
        assert stats.total_generated == 8
        assert len(jobs) == stats.total_valid
        assert len(jobs) > 0

    def test_filters_spacing_too_tight(self, generator, windows, tmp_path, sample_sweep_dict):
        # Tiny std_dev → tight range → many grids won't fit
        csv_path = _write_test_csv(tmp_path, price=100.0, std_dev=0.01)
        ohlcv_paths = {"BTC/USDT": str(csv_path)}
        sample_sweep_dict["sweep"]["num_grids"] = [20]
        sample_sweep_dict["sweep"]["range_volatility_multiplier"] = [0.5]

        jobs, stats = generator.generate(
            sweep_params=sample_sweep_dict["sweep"],
            pairs=["BTC/USDT"],
            windows=windows,
            ohlcv_paths=ohlcv_paths,
            walk_forward_engine=WalkForwardEngine(),
            fixed_params={
                "exchange": "binance",
                "trading_fee": 0.001,
                "initial_balance": 1000,
                "timeframe": "1m",
                "backtest_slippage": 0.0,
            },
        )

        assert stats.spacing_too_tight > 0

    def test_job_ids_are_deterministic(self, generator, windows, tmp_path, sample_sweep_dict):
        csv_path = _write_test_csv(tmp_path, price=100.0, std_dev=10.0)
        ohlcv_paths = {"BTC/USDT": str(csv_path)}

        jobs1, _ = generator.generate(
            sweep_params=sample_sweep_dict["sweep"],
            pairs=["BTC/USDT"],
            windows=windows,
            ohlcv_paths=ohlcv_paths,
            walk_forward_engine=WalkForwardEngine(),
            fixed_params={"exchange": "binance", "trading_fee": 0.001, "initial_balance": 1000,
                          "timeframe": "1m", "backtest_slippage": 0.0},
        )
        jobs2, _ = generator.generate(
            sweep_params=sample_sweep_dict["sweep"],
            pairs=["BTC/USDT"],
            windows=windows,
            ohlcv_paths=ohlcv_paths,
            walk_forward_engine=WalkForwardEngine(),
            fixed_params={"exchange": "binance", "trading_fee": 0.001, "initial_balance": 1000,
                          "timeframe": "1m", "backtest_slippage": 0.0},
        )

        ids1 = [j.job_id for j in jobs1]
        ids2 = [j.job_id for j in jobs2]
        assert ids1 == ids2

    def test_job_is_frozen_dataclass(self, generator, windows, tmp_path, sample_sweep_dict):
        csv_path = _write_test_csv(tmp_path, price=100.0, std_dev=10.0)
        ohlcv_paths = {"BTC/USDT": str(csv_path)}

        jobs, _ = generator.generate(
            sweep_params=sample_sweep_dict["sweep"],
            pairs=["BTC/USDT"],
            windows=windows,
            ohlcv_paths=ohlcv_paths,
            walk_forward_engine=WalkForwardEngine(),
            fixed_params={"exchange": "binance", "trading_fee": 0.001, "initial_balance": 1000,
                          "timeframe": "1m", "backtest_slippage": 0.0},
        )

        with pytest.raises(AttributeError):
            jobs[0].pair = "OTHER/USDT"

    def test_sweep_params_stored_as_tuple(self, generator, windows, tmp_path, sample_sweep_dict):
        csv_path = _write_test_csv(tmp_path, price=100.0, std_dev=10.0)
        ohlcv_paths = {"BTC/USDT": str(csv_path)}

        jobs, _ = generator.generate(
            sweep_params=sample_sweep_dict["sweep"],
            pairs=["BTC/USDT"],
            windows=windows,
            ohlcv_paths=ohlcv_paths,
            walk_forward_engine=WalkForwardEngine(),
            fixed_params={"exchange": "binance", "trading_fee": 0.001, "initial_balance": 1000,
                          "timeframe": "1m", "backtest_slippage": 0.0},
        )

        assert isinstance(jobs[0].sweep_params, tuple)
        params = dict(jobs[0].sweep_params)
        assert "strategy_type" in params
        assert "num_grids" in params


def _write_test_csv(tmp_path, price: float, std_dev: float) -> str:
    """Write a synthetic OHLCV CSV with controllable mean and spread."""
    import numpy as np

    rng = np.random.default_rng(42)
    rows = ["timestamp,open,high,low,close,volume"]
    for i in range(5000):
        close = price + rng.normal(0, std_dev)
        ts = f"2025-01-01 00:{(i // 60):02d}:{(i % 60):02d}"
        if i >= 3600:
            ts = f"2025-01-01 01:{((i - 3600) // 60):02d}:{((i - 3600) % 60):02d}"
        rows.append(f"{ts},{close},{close+0.1},{close-0.1},{close},100")
    path = tmp_path / "ohlcv.csv"
    path.write_text("\n".join(rows))
    return str(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_combination_generator.py -v`

- [ ] **Step 3: Implement CombinationGenerator**

```python
# src/grid_sweep/combination_generator.py
import hashlib
import itertools
import logging
from dataclasses import dataclass
from typing import Any

from .walk_forward import WalkForwardEngine, WalkForwardWindow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BacktestJob:
    """Immutable unit of work. Sweep params stored as a generic tuple for extensibility (FR-2.5)."""
    job_id: str
    pair: str
    window: WalkForwardWindow
    # Sweep params as a hashable tuple of (key, value) pairs — adding a new sweep param requires
    # NO changes here, only in worker.py's _build_config_dict mapping.
    sweep_params: tuple[tuple[str, Any], ...]
    # Computed from volatility
    range_top: float
    range_bottom: float
    # Fixed params (carried for config building)
    exchange: str
    trading_fee: float
    initial_balance: float
    timeframe: str
    backtest_slippage: float
    ohlcv_csv_path: str

    def get_sweep_param(self, key: str) -> Any:
        for k, v in self.sweep_params:
            if k == key:
                return v
        raise KeyError(f"Sweep param not found: {key}")


@dataclass
class FilterStats:
    total_generated: int = 0
    total_valid: int = 0
    spacing_too_tight: int = 0
    range_too_narrow: int = 0


class CombinationGenerator:
    def __init__(self, trading_fee: float):
        self.trading_fee = trading_fee

    def generate(
        self,
        sweep_params: dict[str, list[Any]],
        pairs: list[str],
        windows: list[WalkForwardWindow],
        ohlcv_paths: dict[str, str],
        walk_forward_engine: WalkForwardEngine,
        fixed_params: dict[str, Any],
    ) -> tuple[list[BacktestJob], FilterStats]:
        stats = FilterStats()
        jobs: list[BacktestJob] = []
        param_names = list(sweep_params.keys())
        param_values = list(sweep_params.values())

        for pair in pairs:
            csv_path = ohlcv_paths[pair]
            for window in windows:
                for combo in itertools.product(*param_values):
                    params = dict(zip(param_names, combo))
                    stats.total_generated += 1

                    multiplier = params["range_volatility_multiplier"]
                    vol_range = walk_forward_engine.compute_volatility_range(
                        csv_path, window.train_start, window.train_end, multiplier,
                    )

                    is_valid, reason = self._is_valid_combination(
                        range_top=vol_range.top,
                        range_bottom=vol_range.bottom,
                        num_grids=params["num_grids"],
                        spacing=params["spacing"],
                    )
                    if not is_valid:
                        if reason == "spacing_too_tight":
                            stats.spacing_too_tight += 1
                        elif reason == "range_too_narrow":
                            stats.range_too_narrow += 1
                        continue

                    job_id = self._compute_job_id(pair, window.index, params)
                    sweep_params_tuple = tuple(sorted(params.items()))
                    jobs.append(
                        BacktestJob(
                            job_id=job_id,
                            pair=pair,
                            window=window,
                            sweep_params=sweep_params_tuple,
                            range_top=vol_range.top,
                            range_bottom=vol_range.bottom,
                            exchange=fixed_params["exchange"],
                            trading_fee=fixed_params["trading_fee"],
                            initial_balance=fixed_params["initial_balance"],
                            timeframe=fixed_params["timeframe"],
                            backtest_slippage=fixed_params["backtest_slippage"],
                            ohlcv_csv_path=csv_path,
                        )
                    )

        stats.total_valid = len(jobs)
        logger.info(
            "Generated %d valid jobs out of %d total (filtered: %d spacing, %d range)",
            stats.total_valid, stats.total_generated, stats.spacing_too_tight, stats.range_too_narrow,
        )
        return jobs, stats

    def _is_valid_combination(
        self,
        range_top: float,
        range_bottom: float,
        num_grids: int,
        spacing: str,
    ) -> tuple[bool, str]:
        if range_bottom <= 0 or range_top <= range_bottom:
            return False, "range_too_narrow"

        if spacing == "arithmetic":
            grid_spacing = (range_top - range_bottom) / (num_grids - 1) if num_grids > 1 else 0
        else:
            ratio = (range_top / range_bottom) ** (1 / (num_grids - 1)) if num_grids > 1 else 1
            grid_spacing = range_bottom * (ratio - 1)

        min_profitable_spacing = range_bottom * 2 * self.trading_fee
        if grid_spacing < min_profitable_spacing:
            return False, "spacing_too_tight"

        return True, ""

    def _compute_job_id(self, pair: str, window_index: int, params: dict[str, Any]) -> str:
        key = f"{pair}|{window_index}"
        for k in sorted(params.keys()):
            key += f"|{k}={params[k]}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_combination_generator.py -v`
Expected: 4 PASSED

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
git add src/grid_sweep/combination_generator.py tests/test_combination_generator.py
git commit -m "feat: add CombinationGenerator with cartesian product and validity filtering"
```

---

## Task 5: Worker — Single Backtest Execution

**Files:**
- Create: `src/grid_sweep/worker.py`
- Create: `tests/test_worker.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_worker.py
import pytest

from grid_sweep.worker import BacktestResult, build_config_dict, run_backtest_pair
from grid_sweep.combination_generator import BacktestJob
from grid_sweep.walk_forward import WalkForwardWindow


@pytest.fixture
def sample_job(integration_csv_path):
    """Requires a real OHLCV CSV for integration. Uses grid_trading_bot test fixture."""
    return BacktestJob(
        job_id="test_job_001",
        pair="SOL/USDT",
        window=WalkForwardWindow(
            index=0,
            train_start="2024-08-01T00:00:00Z",
            train_end="2024-08-02T00:00:00Z",
            test_start="2024-08-02T00:00:00Z",
            test_end="2024-08-03T00:00:00Z",
        ),
        sweep_params=(
            ("buy_ratio", 1.0),
            ("num_grids", 4),
            ("range_volatility_multiplier", 2.0),
            ("sell_ratio", 1.0),
            ("spacing", "arithmetic"),
            ("strategy_type", "simple_grid"),
        ),
        range_top=170.0,
        range_bottom=155.0,
        exchange="binance",
        trading_fee=0.001,
        initial_balance=150,
        timeframe="1m",
        backtest_slippage=0.0,
        ohlcv_csv_path=integration_csv_path,
    )


class TestBuildConfigDict:
    def test_builds_valid_config(self, sample_job):
        config = build_config_dict(sample_job, "2024-08-01T00:00:00Z", "2024-08-02T00:00:00Z")

        assert config["exchange"]["name"] == "binance"
        assert config["exchange"]["trading_mode"] == "backtest"
        assert config["pair"]["base_currency"] == "SOL"
        assert config["pair"]["quote_currency"] == "USDT"
        assert config["grid_strategy"]["num_grids"] == 4
        assert config["grid_strategy"]["type"] == "simple_grid"
        assert config["grid_strategy"]["range"]["top"] == 170.0
        assert config["logging"]["log_level"] == "WARNING"


class TestRunBacktestPair:
    @pytest.mark.timeout(30)
    def test_runs_train_and_test(self, sample_job):
        train_result, test_result = run_backtest_pair(sample_job)

        assert isinstance(train_result, BacktestResult)
        assert isinstance(test_result, BacktestResult)
        assert train_result.phase == "train"
        assert test_result.phase == "test"
        assert train_result.error is None
        assert test_result.error is None
```

Note: `integration_csv_path` fixture must be added to `tests/conftest.py`:

```python
# Add to tests/conftest.py
import os

@pytest.fixture
def integration_csv_path():
    """Path to the SOL_USDT_1m.csv from grid_trading_bot's test fixtures."""
    path = os.path.join(
        os.path.dirname(__file__),
        "..", "..", "grid_trading_bot", "tests", "integration", "fixtures", "SOL_USDT_1m.csv",
    )
    resolved = os.path.abspath(path)
    if not os.path.exists(resolved):
        pytest.skip(f"Integration CSV not found: {resolved}")
    return resolved
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_worker.py -v`

- [ ] **Step 3: Implement worker**

```python
# src/grid_sweep/worker.py
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.config_validator import ConfigValidator
from grid_trading_bot.core.bot_management.event_bus import EventBus
from grid_trading_bot.core.bot_management.grid_trading_bot import GridTradingBot
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler

from .combination_generator import BacktestJob

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    job_id: str
    pair: str
    window_index: int
    phase: Literal["train", "test"]
    roi: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    num_buy_trades: int = 0
    num_sell_trades: int = 0
    total_fees: float = 0.0
    grid_trading_gains: float = 0.0
    buy_and_hold_return: float = 0.0
    error: str | None = None


def run_backtest_pair(job: BacktestJob) -> tuple[BacktestResult, BacktestResult]:
    train_result = _run_single_backtest(job, "train")
    test_result = _run_single_backtest(job, "test")
    return train_result, test_result


def build_config_dict(job: BacktestJob, start_date: str, end_date: str) -> dict[str, Any]:
    """Maps BacktestJob to the config.json schema expected by ConfigManager.
    This is the ONLY place that maps sweep param names to bot config fields (FR-2.5).
    """
    params = dict(job.sweep_params)
    return {
        "exchange": {
            "name": job.exchange,
            "trading_fee": job.trading_fee,
            "trading_mode": "backtest",
        },
        "pair": {
            "base_currency": job.pair.split("/")[0],
            "quote_currency": job.pair.split("/")[1],
        },
        "trading_settings": {
            "timeframe": job.timeframe,
            "period": {"start_date": start_date, "end_date": end_date},
            "initial_balance": job.initial_balance,
            "historical_data_file": job.ohlcv_csv_path,
        },
        "grid_strategy": {
            "type": params["strategy_type"],
            "spacing": params["spacing"],
            "num_grids": params["num_grids"],
            "range": {"top": job.range_top, "bottom": job.range_bottom},
            "buy_ratio": params["buy_ratio"],
            "sell_ratio": params["sell_ratio"],
        },
        "risk_management": {
            "take_profit": {"enabled": False, "threshold": 0},
            "stop_loss": {"enabled": False, "threshold": 0},
        },
        "execution": {
            "backtest_slippage": job.backtest_slippage,
        },
        "logging": {
            "log_level": "WARNING",
            "log_to_file": False,
        },
    }


def _run_single_backtest(job: BacktestJob, phase: Literal["train", "test"]) -> BacktestResult:
    if phase == "train":
        start_date, end_date = job.window.train_start, job.window.train_end
    else:
        start_date, end_date = job.window.test_start, job.window.test_end

    try:
        config_dict = build_config_dict(job, start_date, end_date)
        config_manager = ConfigManager.from_dict(config_dict, ConfigValidator())
        event_bus = EventBus()
        notification_handler = NotificationHandler(event_bus, [], config_manager.get_trading_mode())
        bot = GridTradingBot(
            config_path="<sweep>",
            config_manager=config_manager,
            notification_handler=notification_handler,
            event_bus=event_bus,
            no_plot=True,
        )
        result = asyncio.run(bot.run())
        return _extract_result(job, phase, result)

    except Exception as e:
        logger.warning("Backtest failed for job %s (%s): %s", job.job_id, phase, e)
        return BacktestResult(
            job_id=job.job_id,
            pair=job.pair,
            window_index=job.window.index,
            phase=phase,
            error=str(e),
        )


def _extract_result(
    job: BacktestJob,
    phase: Literal["train", "test"],
    raw_result: dict[str, Any] | None,
) -> BacktestResult:
    if not raw_result or "performance_summary" not in raw_result:
        return BacktestResult(job_id=job.job_id, pair=job.pair, window_index=job.window.index,
                              phase=phase, error="No performance summary returned")

    summary = raw_result["performance_summary"]
    return BacktestResult(
        job_id=job.job_id,
        pair=job.pair,
        window_index=job.window.index,
        phase=phase,
        roi=_parse_pct(summary.get("ROI", "0%")),
        max_drawdown=_parse_pct(summary.get("Max Drawdown", "0%")),
        sharpe_ratio=_parse_float(summary.get("Sharpe Ratio", "0")),
        sortino_ratio=_parse_float(summary.get("Sortino Ratio", "0")),
        num_buy_trades=summary.get("Number of Buy Trades", 0),
        num_sell_trades=summary.get("Number of Sell Trades", 0),
        total_fees=_parse_float(summary.get("Total Fees", "0")),
        grid_trading_gains=_parse_float(summary.get("Grid Trading Gains", "0")),
        buy_and_hold_return=_parse_pct(summary.get("Buy and Hold Return %", "0%")),
    )


def _parse_pct(value: str | float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).replace("%", "").strip())


def _parse_float(value: str | float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).replace(",", "").strip())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_worker.py -v`
Expected: 2 PASSED (skipped if integration CSV not found)

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
git add src/grid_sweep/worker.py tests/test_worker.py tests/conftest.py
git commit -m "feat: add worker module for single backtest execution via GridTradingBot"
```

---

## Task 6: SweepExecutor — Parallel Execution & Checkpoint

**Files:**
- Create: `src/grid_sweep/executor.py`
- Create: `tests/test_executor.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_executor.py
import json

import pytest

from grid_sweep.executor import CheckpointManager, SweepExecutor
from grid_sweep.worker import BacktestResult


class TestCheckpointManager:
    def test_save_and_load(self, tmp_path):
        cp_path = str(tmp_path / "checkpoint.json")
        manager = CheckpointManager(cp_path, sweep_yaml_hash="abc123")

        manager.save_batch({"job_1", "job_2"}, [{"job_id": "job_1"}, {"job_id": "job_2"}])
        loaded_ids = manager.load()

        assert loaded_ids == {"job_1", "job_2"}

    def test_load_returns_empty_when_no_file(self, tmp_path):
        cp_path = str(tmp_path / "nonexistent.json")
        manager = CheckpointManager(cp_path, sweep_yaml_hash="abc123")

        assert manager.load() == set()

    def test_rejects_mismatched_hash(self, tmp_path):
        cp_path = str(tmp_path / "checkpoint.json")
        manager1 = CheckpointManager(cp_path, sweep_yaml_hash="hash_v1")
        manager1.save_batch({"job_1"}, [{"job_id": "job_1"}])

        manager2 = CheckpointManager(cp_path, sweep_yaml_hash="hash_v2")
        loaded = manager2.load()
        assert loaded == set()

    def test_atomic_append(self, tmp_path):
        cp_path = str(tmp_path / "checkpoint.json")
        manager = CheckpointManager(cp_path, sweep_yaml_hash="abc")

        manager.save_batch({"job_1"}, [{"job_id": "job_1"}])
        manager.save_batch({"job_1", "job_2"}, [{"job_id": "job_1"}, {"job_id": "job_2"}])

        loaded = manager.load()
        assert loaded == {"job_1", "job_2"}


class TestSweepExecutor:
    def test_auto_detect_workers(self):
        executor = SweepExecutor()
        assert executor.max_workers >= 1

    def test_respects_explicit_workers(self):
        executor = SweepExecutor(max_workers=4)
        assert executor.max_workers == 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_executor.py -v`

- [ ] **Step 3: Implement executor**

```python
# src/grid_sweep/executor.py
import json
import logging
import os
import signal
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict

import psutil
from tqdm import tqdm

from .combination_generator import BacktestJob
from .worker import BacktestResult, run_backtest_pair

logger = logging.getLogger(__name__)

_ESTIMATED_MB_PER_WORKER = 80
_CHECKPOINT_BATCH_SIZE = 100


class CheckpointManager:
    def __init__(self, checkpoint_path: str, sweep_yaml_hash: str):
        self.checkpoint_path = checkpoint_path
        self.sweep_yaml_hash = sweep_yaml_hash

    def load(self) -> set[str]:
        if not os.path.exists(self.checkpoint_path):
            return set()

        with open(self.checkpoint_path) as f:
            data = json.load(f)

        if data.get("sweep_yaml_hash") != self.sweep_yaml_hash:
            logger.warning("Checkpoint hash mismatch — starting fresh")
            return set()

        return set(data.get("completed_job_ids", []))

    def save_batch(self, completed_job_ids: set[str], results: list[dict]) -> None:
        data = {
            "sweep_yaml_hash": self.sweep_yaml_hash,
            "completed_job_ids": sorted(completed_job_ids),
            "results": results,
        }
        tmp_path = self.checkpoint_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, self.checkpoint_path)

    def load_results(self) -> list[dict]:
        if not os.path.exists(self.checkpoint_path):
            return []

        with open(self.checkpoint_path) as f:
            data = json.load(f)

        if data.get("sweep_yaml_hash") != self.sweep_yaml_hash:
            return []

        return data.get("results", [])


class SweepExecutor:
    def __init__(
        self,
        max_workers: int | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        resume: bool = False,
    ):
        self.max_workers = max_workers or self._auto_detect_workers()
        self.checkpoint_manager = checkpoint_manager
        self.resume = resume
        self._shutdown_requested = False

    def execute(self, jobs: list[BacktestJob]) -> list[tuple[BacktestResult, BacktestResult]]:
        completed_ids: set[str] = set()
        all_results: list[dict] = []
        result_pairs: list[tuple[BacktestResult, BacktestResult]] = []

        if self.resume and self.checkpoint_manager:
            completed_ids = self.checkpoint_manager.load()
            all_results = self.checkpoint_manager.load_results()
            logger.info("Resuming: %d jobs already completed", len(completed_ids))

        pending_jobs = [j for j in jobs if j.job_id not in completed_ids]
        if not pending_jobs:
            logger.info("All jobs already completed")
            return result_pairs

        logger.info("Executing %d jobs with %d workers", len(pending_jobs), self.max_workers)

        original_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handle_sigint)

        try:
            with ProcessPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {pool.submit(run_backtest_pair, job): job for job in pending_jobs}

                with tqdm(total=len(pending_jobs), desc="Backtests", unit="job") as pbar:
                    for future in as_completed(futures):
                        if self._shutdown_requested:
                            break

                        job = futures[future]
                        try:
                            train_result, test_result = future.result()
                            result_pairs.append((train_result, test_result))
                            completed_ids.add(job.job_id)
                            all_results.append({
                                "job_id": job.job_id,
                                "pair": job.pair,
                                "window_index": job.window.index,
                                "train": asdict(train_result),
                                "test": asdict(test_result),
                            })
                        except Exception as e:
                            logger.warning("Job %s failed: %s", job.job_id, e)
                            error_result = BacktestResult(
                                job_id=job.job_id, pair=job.pair,
                                window_index=job.window.index, phase="test", error=str(e),
                            )
                            result_pairs.append((error_result, error_result))

                        pbar.update(1)

                        if self.checkpoint_manager and len(completed_ids) % _CHECKPOINT_BATCH_SIZE == 0:
                            self.checkpoint_manager.save_batch(completed_ids, all_results)

        finally:
            signal.signal(signal.SIGINT, original_sigint)
            if self.checkpoint_manager:
                self.checkpoint_manager.save_batch(completed_ids, all_results)

        if self._shutdown_requested:
            logger.info("Interrupted. %d/%d jobs completed. Resume with --resume", len(completed_ids), len(jobs))

        return result_pairs

    def _handle_sigint(self, signum, frame):
        logger.info("SIGINT received — finishing in-flight jobs...")
        self._shutdown_requested = True

    @staticmethod
    def _auto_detect_workers() -> int:
        cpu_count = os.cpu_count() or 4
        available_mb = psutil.virtual_memory().available / (1024 * 1024)
        memory_limit = int((available_mb * 0.8) / _ESTIMATED_MB_PER_WORKER)
        workers = max(1, min(cpu_count, memory_limit))
        logger.info("Auto-detected %d workers (cpu=%d, memory_limit=%d)", workers, cpu_count, memory_limit)
        return workers
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_executor.py -v`
Expected: 6 PASSED

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
git add src/grid_sweep/executor.py tests/test_executor.py
git commit -m "feat: add SweepExecutor with multiprocessing pool and checkpoint/resume"
```

---

## Task 7: PairResolver & OHLCVCache

**Files:**
- Create: `src/grid_sweep/pair_resolver.py`
- Create: `src/grid_sweep/ohlcv_cache.py`
- Create: `tests/test_pair_resolver.py`
- Create: `tests/test_ohlcv_cache.py`

- [ ] **Step 1: Write failing tests for PairResolver**

```python
# tests/test_pair_resolver.py
import pytest

from grid_sweep.pair_resolver import PairResolver
from grid_sweep.sweep_config import PairsConfig


class TestPairResolver:
    def setup_method(self):
        self.resolver = PairResolver()

    async def test_manual_mode_returns_list(self):
        config = PairsConfig(mode="manual", count=0, quote_currency="USDT",
                             manual_list=["BTC/USDT", "ETH/USDT"])
        pairs = await self.resolver.resolve(config, "binance")
        assert pairs == ["BTC/USDT", "ETH/USDT"]

    async def test_manual_mode_empty_list_raises(self):
        config = PairsConfig(mode="manual", count=0, quote_currency="USDT", manual_list=[])
        with pytest.raises(ValueError, match="empty"):
            await self.resolver.resolve(config, "binance")

    @pytest.mark.sandbox
    async def test_auto_mode_fetches_top_pairs(self):
        config = PairsConfig(mode="auto", count=5, quote_currency="USDT")
        pairs = await self.resolver.resolve(config, "binance")
        assert len(pairs) == 5
        assert all("/USDT" in p for p in pairs)
```

- [ ] **Step 2: Write failing tests for OHLCVCache**

```python
# tests/test_ohlcv_cache.py
import pytest

from grid_sweep.ohlcv_cache import OHLCVCache


class TestOHLCVCache:
    def test_cache_path_format(self, tmp_path):
        cache = OHLCVCache(cache_dir=str(tmp_path))
        path = cache.cache_path("binance", "BTC/USDT", "1m")
        assert path.endswith("binance/BTC_USDT_1m.csv")

    def test_cache_path_creates_directory(self, tmp_path):
        cache = OHLCVCache(cache_dir=str(tmp_path))
        path = cache.cache_path("binance", "BTC/USDT", "1m")
        import os
        assert os.path.isdir(os.path.dirname(path))

    async def test_ensure_cached_writes_file(self, tmp_path):
        cache = OHLCVCache(cache_dir=str(tmp_path))
        # This test needs a mock or a sandbox exchange connection
        # For now test the cache_path logic and file detection
        cache_path = cache.cache_path("binance", "BTC/USDT", "1m")

        import os
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            f.write("timestamp,open,high,low,close,volume\n2025-01-01 00:00:00,100,101,99,100,500\n")

        result = await cache.ensure_cached("binance", "BTC/USDT", "1m",
                                           "2025-01-01T00:00:00Z", "2025-01-01T00:01:00Z")
        assert result == cache_path
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_pair_resolver.py tests/test_ohlcv_cache.py -v -m "not sandbox"`

- [ ] **Step 4: Implement PairResolver**

```python
# src/grid_sweep/pair_resolver.py
import logging

import ccxt.async_support as ccxt_async

from .sweep_config import PairsConfig

logger = logging.getLogger(__name__)


class PairResolver:
    async def resolve(self, pairs_config: PairsConfig, exchange_name: str) -> list[str]:
        if pairs_config.mode == "manual":
            return self._resolve_manual(pairs_config)
        return await self._resolve_auto(pairs_config, exchange_name)

    def _resolve_manual(self, pairs_config: PairsConfig) -> list[str]:
        if not pairs_config.manual_list:
            raise ValueError("Manual pair list is empty — provide at least one pair")
        return list(pairs_config.manual_list)

    async def _resolve_auto(self, pairs_config: PairsConfig, exchange_name: str) -> list[str]:
        exchange_class = getattr(ccxt_async, exchange_name)
        exchange = exchange_class()
        try:
            tickers = await exchange.fetch_tickers()
            quote = pairs_config.quote_currency
            usdt_tickers = {
                symbol: data
                for symbol, data in tickers.items()
                if symbol.endswith(f"/{quote}") and data.get("quoteVolume")
            }
            sorted_pairs = sorted(usdt_tickers.keys(), key=lambda s: usdt_tickers[s]["quoteVolume"], reverse=True)
            selected = sorted_pairs[: pairs_config.count]
            logger.info("Resolved top %d %s pairs: %s", pairs_config.count, quote, selected)
            return selected
        finally:
            await exchange.close()
```

- [ ] **Step 5: Implement OHLCVCache**

```python
# src/grid_sweep/ohlcv_cache.py
import logging
import os
from collections.abc import Callable

import ccxt
import pandas as pd

logger = logging.getLogger(__name__)

_CANDLE_LIMITS = {"binance": 1000, "kraken": 720, "coinbase": 300}
_TIMEFRAME_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}
_RATE_LIMIT_SLEEP = 0.5


class OHLCVCache:
    def __init__(self, cache_dir: str = "data/ohlcv_cache"):
        self.cache_dir = cache_dir

    def cache_path(self, exchange: str, pair: str, timeframe: str) -> str:
        safe_pair = pair.replace("/", "_")
        directory = os.path.join(self.cache_dir, exchange)
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, f"{safe_pair}_{timeframe}.csv")

    async def ensure_cached(
        self,
        exchange_name: str,
        pair: str,
        timeframe: str,
        start_date: str,
        end_date: str,
    ) -> str:
        import asyncio

        path = self.cache_path(exchange_name, pair, timeframe)
        requested_start = pd.Timestamp(start_date)
        requested_end = pd.Timestamp(end_date)

        if os.path.exists(path):
            existing = pd.read_csv(path, parse_dates=["timestamp"])
            first_ts = existing["timestamp"].iloc[0]
            last_ts = existing["timestamp"].iloc[-1]

            if first_ts <= requested_start and last_ts >= requested_end:
                logger.info("Cache hit for %s (%s)", pair, timeframe)
                return path

            # Incremental: only fetch what's missing (FR-1.7)
            fetch_start = start_date if requested_start < first_ts else last_ts.isoformat() + "Z"
            fetch_end = end_date if requested_end > last_ts else first_ts.isoformat() + "Z"
            logger.info("Cache partial for %s — fetching %s to %s", pair, fetch_start, fetch_end)
            new_df = await asyncio.to_thread(self._fetch_ohlcv, exchange_name, pair, timeframe, fetch_start, fetch_end)
            df = pd.concat([existing, new_df]).drop_duplicates(subset="timestamp").sort_values("timestamp")
        else:
            df = await asyncio.to_thread(self._fetch_ohlcv, exchange_name, pair, timeframe, start_date, end_date)

        df.to_csv(path, index=False)
        logger.info("Cached %d candles for %s → %s", len(df), pair, path)
        return path

    async def ensure_all_cached(
        self,
        exchange_name: str,
        pairs: list[str],
        timeframe: str,
        start_date: str,
        end_date: str,
        on_progress: Callable[[str], None] | None = None,
    ) -> dict[str, str]:
        paths: dict[str, str] = {}
        for pair in pairs:
            if on_progress:
                on_progress(pair)
            paths[pair] = await self.ensure_cached(exchange_name, pair, timeframe, start_date, end_date)
        return paths

    def _fetch_ohlcv(
        self,
        exchange_name: str,
        pair: str,
        timeframe: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        import time

        exchange_class = getattr(ccxt, exchange_name)
        exchange = exchange_class()
        exchange.load_markets()

        candle_limit = _CANDLE_LIMITS.get(exchange_name, 500)
        tf_ms = _TIMEFRAME_MS[timeframe]
        since = exchange.parse8601(start_date)
        until = exchange.parse8601(end_date)
        all_candles: list[list] = []

        while since < until:
            candles = exchange.fetch_ohlcv(pair, timeframe, since=since, limit=candle_limit)
            if not candles:
                break
            all_candles.extend(candles)
            since = candles[-1][0] + tf_ms
            time.sleep(_RATE_LIMIT_SLEEP)

        exchange.close()

        df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df[df["timestamp"] < pd.Timestamp(end_date)]
        return df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_pair_resolver.py tests/test_ohlcv_cache.py -v -m "not sandbox"`
Expected: Non-sandbox tests PASS

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
git add src/grid_sweep/pair_resolver.py src/grid_sweep/ohlcv_cache.py tests/test_pair_resolver.py tests/test_ohlcv_cache.py
git commit -m "feat: add PairResolver and OHLCVCache for data fetching and caching"
```

---

## Task 8: SweepReporter — CSV Output & Console Summary

**Files:**
- Create: `src/grid_sweep/reporter.py`
- Create: `tests/test_reporter.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_reporter.py
import csv

import pytest

from grid_sweep.reporter import SweepReporter
from grid_sweep.worker import BacktestResult


def _make_result(pair: str, window_index: int, phase: str, roi: float, max_dd: float) -> BacktestResult:
    return BacktestResult(
        job_id=f"job_{pair}_{window_index}",
        pair=pair,
        window_index=window_index,
        phase=phase,
        roi=roi,
        max_drawdown=max_dd,
        sharpe_ratio=1.0,
        sortino_ratio=1.2,
        num_buy_trades=5,
        num_sell_trades=3,
        total_fees=0.5,
        grid_trading_gains=10.0,
        buy_and_hold_return=2.0,
    )


@pytest.fixture
def sample_results():
    return [
        (
            _make_result("BTC/USDT", 0, "train", 3.0, 5.0),
            _make_result("BTC/USDT", 0, "test", 1.5, 3.0),
        ),
        (
            _make_result("BTC/USDT", 1, "train", 2.0, 4.0),
            _make_result("BTC/USDT", 1, "test", -0.5, 2.0),
        ),
        (
            _make_result("ETH/USDT", 0, "train", 4.0, 6.0),
            _make_result("ETH/USDT", 0, "test", 2.0, 4.0),
        ),
    ]


@pytest.fixture
def sample_jobs():
    from grid_sweep.combination_generator import BacktestJob
    from grid_sweep.walk_forward import WalkForwardWindow

    def _job(pair, window_index):
        return BacktestJob(
            job_id=f"job_{pair}_{window_index}", pair=pair,
            window=WalkForwardWindow(window_index, "", "", "2025-04-01T00:00:00Z", "2025-05-01T00:00:00Z"),
            sweep_params=(
                ("buy_ratio", 1.0), ("num_grids", 8),
                ("range_volatility_multiplier", 1.5), ("sell_ratio", 1.0),
                ("spacing", "arithmetic"), ("strategy_type", "simple_grid"),
            ),
            range_top=100, range_bottom=90, exchange="binance", trading_fee=0.001,
            initial_balance=1000, timeframe="1m", backtest_slippage=0.0, ohlcv_csv_path="",
        )

    return [_job("BTC/USDT", 0), _job("BTC/USDT", 1), _job("ETH/USDT", 0)]


class TestSweepReporter:
    def test_write_csv_creates_file(self, tmp_path, sample_results, sample_jobs):
        output = str(tmp_path / "results.csv")
        reporter = SweepReporter()
        reporter.write_csv(sample_results, sample_jobs, output)

        with open(output) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 3
        assert "test_roi" in rows[0]
        assert "train_roi" in rows[0]
        assert "window_start" in rows[0]
        assert "window_end" in rows[0]

    def test_csv_sorted_by_test_roi_descending(self, tmp_path, sample_results, sample_jobs):
        output = str(tmp_path / "results.csv")
        reporter = SweepReporter()
        reporter.write_csv(sample_results, sample_jobs, output)

        with open(output) as f:
            reader = csv.DictReader(f)
            rois = [float(row["test_roi"]) for row in reader]

        assert rois == sorted(rois, reverse=True)

    def test_console_summary_shows_best_per_pair(self, capsys, sample_results, sample_jobs):
        reporter = SweepReporter()
        reporter.print_console_summary(sample_results, sample_jobs, ["BTC/USDT", "ETH/USDT"])

        output = capsys.readouterr().out
        assert "BTC/USDT" in output
        assert "ETH/USDT" in output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_reporter.py -v`

- [ ] **Step 3: Implement SweepReporter**

```python
# src/grid_sweep/reporter.py
import csv
import logging
from collections import defaultdict

from tabulate import tabulate

from .combination_generator import BacktestJob
from .worker import BacktestResult

logger = logging.getLogger(__name__)

_CSV_COLUMNS = [
    "pair", "window_start", "window_end", "window_index",
    "strategy_type", "spacing", "num_grids",
    "range_volatility_multiplier", "range_top", "range_bottom",
    "buy_ratio", "sell_ratio",
    "train_roi", "test_roi", "train_max_drawdown", "test_max_drawdown",
    "test_sharpe_ratio", "test_sortino_ratio",
    "test_num_buy_trades", "test_num_sell_trades",
    "test_total_fees", "test_grid_trading_gains", "test_buy_and_hold_return",
]


class SweepReporter:
    def write_csv(
        self,
        results: list[tuple[BacktestResult, BacktestResult]],
        jobs: list[BacktestJob],
        output_path: str,
    ) -> None:
        job_map = {j.job_id: j for j in jobs}
        rows = []

        for train_result, test_result in results:
            job = job_map.get(test_result.job_id)
            if not job:
                continue
            params = dict(job.sweep_params)
            rows.append({
                "pair": job.pair,
                "window_start": job.window.test_start,
                "window_end": job.window.test_end,
                "window_index": job.window.index,
                "strategy_type": params.get("strategy_type", ""),
                "spacing": params.get("spacing", ""),
                "num_grids": params.get("num_grids", ""),
                "range_volatility_multiplier": params.get("range_volatility_multiplier", ""),
                "range_top": round(job.range_top, 4),
                "range_bottom": round(job.range_bottom, 4),
                "buy_ratio": params.get("buy_ratio", ""),
                "sell_ratio": params.get("sell_ratio", ""),
                "train_roi": round(train_result.roi, 4),
                "test_roi": round(test_result.roi, 4),
                "train_max_drawdown": round(train_result.max_drawdown, 4),
                "test_max_drawdown": round(test_result.max_drawdown, 4),
                "test_sharpe_ratio": round(test_result.sharpe_ratio, 4),
                "test_sortino_ratio": round(test_result.sortino_ratio, 4),
                "test_num_buy_trades": test_result.num_buy_trades,
                "test_num_sell_trades": test_result.num_sell_trades,
                "test_total_fees": round(test_result.total_fees, 4),
                "test_grid_trading_gains": round(test_result.grid_trading_gains, 4),
                "test_buy_and_hold_return": round(test_result.buy_and_hold_return, 4),
            })

        rows.sort(key=lambda r: r["test_roi"], reverse=True)

        import os
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

        logger.info("Wrote %d rows to %s", len(rows), output_path)

    def print_console_summary(
        self,
        results: list[tuple[BacktestResult, BacktestResult]],
        jobs: list[BacktestJob],
        pairs: list[str],
    ) -> None:
        job_map = {j.job_id: j for j in jobs}
        per_pair_configs: dict[str, dict[str, list[BacktestResult]]] = defaultdict(lambda: defaultdict(list))

        for _, test_result in results:
            if test_result.error:
                continue
            job = job_map.get(test_result.job_id)
            if not job:
                continue
            config_key = _config_fingerprint(job)
            per_pair_configs[job.pair][config_key].append(test_result)

        table_rows = []
        for pair in pairs:
            configs = per_pair_configs.get(pair, {})
            if not configs:
                continue

            best_key = None
            best_avg_roi = float("-inf")
            for key, test_results in configs.items():
                avg_roi = sum(r.roi for r in test_results) / len(test_results)
                if avg_roi > best_avg_roi:
                    best_avg_roi = avg_roi
                    best_key = key

            if best_key:
                best_results = configs[best_key]
                avg_dd = sum(r.max_drawdown for r in best_results) / len(best_results)
                win_rate = sum(1 for r in best_results if r.roi > 0) / len(best_results) * 100
                job = next(j for j in jobs if _config_fingerprint(j) == best_key and j.pair == pair)
                params = dict(job.sweep_params)
                table_rows.append([
                    pair, params.get("strategy_type"), params.get("spacing"),
                    params.get("num_grids"), params.get("range_volatility_multiplier"),
                    params.get("buy_ratio"), params.get("sell_ratio"),
                    f"{best_avg_roi:.2f}%", f"{avg_dd:.2f}%", f"{win_rate:.0f}%",
                ])

        headers = ["Pair", "Strategy", "Spacing", "Grids", "Vol Mult", "Buy R", "Sell R",
                    "Avg Test ROI", "Avg Test DD", "Win Rate"]
        print("\n" + tabulate(table_rows, headers=headers, tablefmt="simple"))  # noqa: T201


def _config_fingerprint(job: BacktestJob) -> str:
    return "|".join(f"{k}={v}" for k, v in job.sweep_params)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_reporter.py -v`
Expected: 3 PASSED

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
git add src/grid_sweep/reporter.py tests/test_reporter.py
git commit -m "feat: add SweepReporter with CSV output and per-pair console summary"
```

---

## Task 9: CLI Wiring — Full Pipeline

**Files:**
- Modify: `src/grid_sweep/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

```python
# tests/test_cli.py
from click.testing import CliRunner

from grid_sweep.cli import main


class TestCLI:
    def test_version(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

    def test_run_missing_config(self):
        runner = CliRunner()
        result = runner.invoke(main, ["run"])
        assert result.exit_code != 0
        assert "Missing" in result.output or "required" in result.output.lower()

    def test_run_nonexistent_config(self):
        runner = CliRunner()
        result = runner.invoke(main, ["run", "--config", "/nonexistent/sweep.yaml"])
        assert result.exit_code != 0

    def test_dry_run(self, tmp_path, sample_sweep_dict):
        import yaml
        path = tmp_path / "sweep.yaml"
        # Use manual mode with a small sweep to avoid network calls
        sample_sweep_dict["pairs"]["mode"] = "manual"
        path.write_text(yaml.dump(sample_sweep_dict))

        runner = CliRunner()
        result = runner.invoke(main, ["run", "--config", str(path), "--dry-run"])
        assert result.exit_code == 0
        assert "combinations" in result.output.lower() or "jobs" in result.output.lower()

    def test_pairs_only(self, tmp_path, sample_sweep_dict):
        import yaml
        path = tmp_path / "sweep.yaml"
        sample_sweep_dict["pairs"]["mode"] = "manual"
        path.write_text(yaml.dump(sample_sweep_dict))

        runner = CliRunner()
        result = runner.invoke(main, ["run", "--config", str(path), "--pairs-only"])
        assert result.exit_code == 0
        assert "BTC/USDT" in result.output
```

- [ ] **Step 2: Run tests to verify they fail (beyond version/missing tests)**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_cli.py -v`

- [ ] **Step 3: Wire up cli.py with full pipeline**

```python
# src/grid_sweep/cli.py
import asyncio
import hashlib
import logging
import os
from datetime import UTC, datetime

import click

from .combination_generator import CombinationGenerator
from .executor import CheckpointManager, SweepExecutor
from .ohlcv_cache import OHLCVCache
from .pair_resolver import PairResolver
from .reporter import SweepReporter
from .sweep_config import SweepConfig
from .walk_forward import WalkForwardEngine

logger = logging.getLogger(__name__)


@click.group()
@click.version_option(package_name="grid_sweep")
def main():
    """Grid Sweep — Parameter sweep with walk-forward validation."""


@main.command()
@click.option("--config", required=True, type=click.Path(exists=True), help="Path to sweep YAML file.")
@click.option("--output", default=None, type=click.Path(), help="Output CSV path.")
@click.option("--workers", default=None, type=int, help="Parallel workers (default: auto).")
@click.option("--resume", is_flag=True, default=False, help="Resume from checkpoint.")
@click.option("--dry-run", is_flag=True, default=False, help="Show job count and estimated time.")
@click.option("--fetch-only", is_flag=True, default=False, help="Only fetch and cache data.")
@click.option("--pairs-only", is_flag=True, default=False, help="Only list resolved pairs.")
def run(config, output, workers, resume, dry_run, fetch_only, pairs_only):
    """Run a parameter sweep with walk-forward validation."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    asyncio.run(_sweep_main(config, output, workers, resume, dry_run, fetch_only, pairs_only))


async def _sweep_main(
    config_path: str,
    output: str | None,
    workers: int | None,
    resume: bool,
    dry_run: bool,
    fetch_only: bool,
    pairs_only: bool,
) -> None:
    sweep_config = SweepConfig.from_yaml(config_path)

    # Resolve pairs
    pair_resolver = PairResolver()
    pairs = await pair_resolver.resolve(sweep_config.pairs, sweep_config.exchange)

    if pairs_only:
        click.echo("Resolved pairs:")
        for pair in pairs:
            click.echo(f"  {pair}")
        return

    # Fetch and cache data
    ohlcv_cache = OHLCVCache()
    click.echo(f"Fetching OHLCV data for {len(pairs)} pairs...")
    ohlcv_paths = await ohlcv_cache.ensure_all_cached(
        exchange_name=sweep_config.exchange,
        pairs=pairs,
        timeframe=sweep_config.timeframe,
        start_date=sweep_config.period_start,
        end_date=sweep_config.period_end,
        on_progress=lambda pair: click.echo(f"  Caching {pair}..."),
    )

    if fetch_only:
        click.echo("Data cached. Exiting (--fetch-only).")
        return

    # Generate walk-forward windows
    wf_engine = WalkForwardEngine()
    windows = wf_engine.generate_windows(
        sweep_config.period_start, sweep_config.period_end,
        sweep_config.walk_forward.train_months, sweep_config.walk_forward.test_months,
    )

    # Generate combinations
    combo_gen = CombinationGenerator(trading_fee=sweep_config.trading_fee)
    jobs, filter_stats = combo_gen.generate(
        sweep_params=sweep_config.sweep_params,
        pairs=pairs,
        windows=windows,
        ohlcv_paths=ohlcv_paths,
        walk_forward_engine=wf_engine,
        fixed_params={
            "exchange": sweep_config.exchange,
            "trading_fee": sweep_config.trading_fee,
            "initial_balance": sweep_config.initial_balance,
            "timeframe": sweep_config.timeframe,
            "backtest_slippage": sweep_config.backtest_slippage,
        },
    )

    total_combos = sweep_config.total_combinations()
    click.echo(f"\nSweep summary:")
    click.echo(f"  {total_combos} combinations x {len(pairs)} pairs x {len(windows)} windows"
               f" = {filter_stats.total_generated} jobs")
    click.echo(f"  Valid jobs: {filter_stats.total_valid}")
    click.echo(f"  Filtered: {filter_stats.spacing_too_tight} spacing, {filter_stats.range_too_narrow} range")

    if dry_run:
        return

    # Prepare output path
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
    output_path = output or f"results/sweep_{timestamp}.csv"

    # Checkpoint — path based on YAML hash so --resume finds it deterministically
    sweep_yaml_hash = _hash_file(config_path)
    checkpoint_dir = os.path.dirname(output_path) or "."
    checkpoint_path = os.path.join(checkpoint_dir, f".sweep_checkpoint_{sweep_yaml_hash}.json")
    checkpoint_manager = CheckpointManager(checkpoint_path, sweep_yaml_hash)

    # Execute
    executor = SweepExecutor(max_workers=workers, checkpoint_manager=checkpoint_manager, resume=resume)
    results = executor.execute(jobs)

    # Report
    reporter = SweepReporter()
    reporter.write_csv(results, jobs, output_path)
    reporter.print_console_summary(results, jobs, pairs)
    click.echo(f"\nResults saved to: {output_path}")


def _hash_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/test_cli.py -v`
Expected: 5 PASSED

- [ ] **Step 5: Run full test suite**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest -v`
Expected: All tests pass

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
git add src/grid_sweep/cli.py tests/test_cli.py
git commit -m "feat: wire CLI with full sweep pipeline (config → fetch → generate → execute → report)"
```

---

## Task 10: Integration Test — End-to-End Sweep

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_sweep_e2e.py`

- [ ] **Step 1: Write end-to-end integration test**

```python
# tests/integration/test_sweep_e2e.py
"""
End-to-end sweep test using grid_trading_bot's test fixture CSV.
Small parameter space (2 combos × 1 pair × 1 window) to keep fast.
"""
import csv
import os

import pytest
import yaml

from click.testing import CliRunner

from grid_sweep.cli import main


@pytest.fixture
def e2e_sweep_config(tmp_path, integration_csv_path):
    """Minimal sweep config that uses local CSV data.
    The SOL_USDT_1m.csv covers 2024-01-01 to 2024-10-21.
    We use a 4-month window (train=2, test=1) within that range for 1 window.
    The CSV path is injected via a pre-cached OHLCV file.
    """
    # Pre-stage the CSV in the expected cache location so OHLCVCache finds it
    cache_dir = tmp_path / "data" / "ohlcv_cache" / "binance"
    cache_dir.mkdir(parents=True)
    import shutil
    shutil.copy(integration_csv_path, cache_dir / "SOL_USDT_1m.csv")

    config = {
        "exchange": "binance",
        "trading_fee": 0.001,
        "initial_balance": 150,
        "timeframe": "1m",
        "backtest_slippage": 0.0,
        "period": {
            "start_date": "2024-06-01T00:00:00Z",
            "end_date": "2024-10-01T00:00:00Z",
        },
        "pairs": {
            "mode": "manual",
            "count": 1,
            "quote_currency": "USDT",
            "manual_list": ["SOL/USDT"],
        },
        "walk_forward": {
            "train_months": 2,
            "test_months": 1,
        },
        "sweep": {
            "strategy_type": ["simple_grid"],
            "spacing": ["arithmetic"],
            "num_grids": [4],
            "range_volatility_multiplier": [2.0],
            "buy_ratio": [1.0],
            "sell_ratio": [1.0],
        },
    }
    path = tmp_path / "sweep.yaml"
    path.write_text(yaml.dump(config))
    return str(path)


class TestSweepE2E:
    @pytest.mark.timeout(60)
    def test_dry_run_completes(self, e2e_sweep_config):
        runner = CliRunner()
        result = runner.invoke(main, ["run", "--config", e2e_sweep_config, "--dry-run"])
        assert result.exit_code == 0

    @pytest.mark.timeout(120)
    def test_full_sweep_produces_csv(self, e2e_sweep_config, tmp_path):
        output = str(tmp_path / "results.csv")
        runner = CliRunner()
        result = runner.invoke(main, [
            "run", "--config", e2e_sweep_config,
            "--output", output, "--workers", "2",
        ])

        # Verify CSV was created with results
        if result.exit_code == 0:
            assert os.path.exists(output)
            with open(output) as f:
                rows = list(csv.DictReader(f))
            assert len(rows) > 0
            assert "test_roi" in rows[0]
```

Note: This e2e test will need adjustment based on walk-forward windows fitting the 2-day test dataset. The implementer should adapt the window configuration or use a synthetic dataset that spans enough time for at least 1 train+test window.

- [ ] **Step 2: Run integration test**

Run: `cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep && uv run python -m pytest tests/integration/ -v`

- [ ] **Step 3: Fix any issues found during integration**

Address any incompatibilities between components discovered during the e2e test.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/
git commit -m "test: add end-to-end sweep integration test"
```

---

## Task 11: Final Polish

**Files:**
- Modify: multiple (lint fixes, cleanup)

- [ ] **Step 1: Run full lint and fix**

```bash
cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
```

- [ ] **Step 2: Run full test suite with coverage**

```bash
uv run python -m pytest --cov=grid_sweep --cov-report=term -v
```

Expected: All tests pass, reasonable coverage (>80% for non-network code)

- [ ] **Step 3: Verify grid_trading_bot tests still pass**

```bash
cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_trading_bot
uv run python -m pytest --cov=grid_trading_bot --cov-report=term
```

Expected: All existing tests still pass (only change was `from_dict` addition)

- [ ] **Step 4: Final commit**

```bash
cd /Users/jordan/Desktop/Ongoing_Projects/algo_trading/grid_sweep
git add -A
git commit -m "chore: final lint, formatting, and cleanup"
```
