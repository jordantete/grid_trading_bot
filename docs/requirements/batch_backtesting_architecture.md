# Batch Backtesting — Architecture Design

## 1. Overview

The sweep system is a **separate package** (`grid_sweep`) that consumes `grid_trading_bot` as a dependency. It orchestrates existing bot infrastructure without reimplementing any trading logic — it generates configs, manages data, dispatches backtests in parallel, and aggregates results.

**Design principle:** The bot is a library that trades. The sweep is a tool that uses the library. They live in the same monorepo but are independently installable, testable, and versionable.

```
grid_sweep CLI
 │
 ├─ SweepConfig          ← parses sweep.yaml
 ├─ PairResolver         ← resolves top-N pairs via CCXT
 ├─ OHLCVCache           ← fetches & caches data as CSV
 ├─ WalkForwardEngine    ← generates rolling windows + volatility ranges
 ├─ CombinationGenerator ← cartesian product + filtering
 ├─ SweepExecutor        ← multiprocessing worker pool + checkpoint
 │   └─ worker()         ← runs single backtest via grid_trading_bot.GridTradingBot
 └─ SweepReporter        ← CSV output + console summary
```

---

## 2. Monorepo Layout

```
algo_trading/
├── grid_trading_bot/                    # EXISTING — the bot (library)
│   ├── src/grid_trading_bot/
│   │   ├── config/
│   │   │   └── config_manager.py        # MODIFIED: add from_dict() classmethod
│   │   ├── core/
│   │   ├── strategies/
│   │   ├── utils/
│   │   └── cli.py                       # UNCHANGED
│   ├── tests/
│   ├── pyproject.toml
│   └── CLAUDE.md
│
└── grid_sweep/                          # NEW — the sweep orchestrator
    ├── src/grid_sweep/
    │   ├── __init__.py
    │   ├── cli.py                       # Click CLI: `grid_sweep`
    │   ├── sweep_config.py              # YAML parsing → SweepConfig dataclass
    │   ├── pair_resolver.py             # Top-N pairs by volume via CCXT
    │   ├── ohlcv_cache.py              # Fetch + cache + incremental update
    │   ├── walk_forward.py              # Rolling window generation + volatility calc
    │   ├── combination_generator.py     # Cartesian product + validation filter
    │   ├── executor.py                  # Multiprocessing orchestration + checkpoint
    │   ├── worker.py                    # Single backtest execution (subprocess)
    │   └── reporter.py                  # CSV writer + console summary
    ├── tests/
    ├── config/
    │   └── sweep.yaml                   # Example sweep configuration
    ├── pyproject.toml                   # depends on grid_trading_bot @ file:../grid_trading_bot
    └── CLAUDE.md
```

### Dependency Relationship

```
grid_sweep ──depends on──▶ grid_trading_bot
```

`grid_sweep` imports from `grid_trading_bot`:
- `ConfigManager` (+ new `from_dict` classmethod)
- `ConfigValidator`
- `GridTradingBot`
- `EventBus`
- `NotificationHandler`
- `TradingMode`

`grid_trading_bot` has **zero knowledge** of `grid_sweep`.

---

## 3. Component Design

### 3.1 SweepConfig (`sweep_config.py`)

Parses the sweep YAML into a typed dataclass. Separates fixed params from sweep params.

```python
@dataclass(frozen=True)
class PairsConfig:
    mode: Literal["auto", "manual"]  # auto = top N by volume
    count: int                        # used when mode=auto
    quote_currency: str               # e.g. "USDT"
    manual_list: list[str] | None     # used when mode=manual

@dataclass(frozen=True)
class WalkForwardConfig:
    train_months: int
    test_months: int

@dataclass(frozen=True)
class SweepConfig:
    # Fixed params
    exchange: str
    trading_fee: float
    initial_balance: float
    timeframe: str
    backtest_slippage: float
    period_start: str                 # ISO 8601
    period_end: str                   # ISO 8601

    # Data config
    pairs: PairsConfig
    walk_forward: WalkForwardConfig

    # Sweep params: dict of param_name → list of values
    # e.g. {"strategy_type": ["simple_grid", "hedged_grid"], "num_grids": [4, 8, 12]}
    sweep_params: dict[str, list[Any]]

    @classmethod
    def from_yaml(cls, path: str) -> "SweepConfig":
        """Parse YAML, normalize scalars to single-element lists in sweep section."""

    def total_combinations(self) -> int:
        """Product of all sweep param list lengths."""
```

**Extensibility (FR-2.5):** `sweep_params` is a generic dict. Adding `"new_param": [val1, val2]` in YAML automatically flows into the cartesian product. The only code that needs to know about specific param names is the config-to-bot-config mapper in `worker.py`.

### 3.2 PairResolver (`pair_resolver.py`)

Resolves which pairs to sweep.

```python
class PairResolver:
    async def resolve(self, pairs_config: PairsConfig, exchange_name: str) -> list[str]:
        """
        Returns list of pairs like ["BTC/USDT", "ETH/USDT", ...].
        - mode=auto: fetch tickers, sort by quoteVolume, take top N
        - mode=manual: return manual_list directly
        """
```

Uses CCXT `exchange.fetch_tickers()` for volume data. One API call, no rate limit concern.

### 3.3 OHLCVCache (`ohlcv_cache.py`)

Manages local CSV cache of historical data. This runs **once** before any backtests.

```python
class OHLCVCache:
    def __init__(self, cache_dir: str = "data/ohlcv_cache"):
        self.cache_dir = cache_dir

    def cache_path(self, exchange: str, pair: str, timeframe: str) -> str:
        """e.g. data/ohlcv_cache/binance/BTC_USDT_1m.csv"""

    async def ensure_cached(
        self,
        exchange_name: str,
        pair: str,
        timeframe: str,
        start_date: str,
        end_date: str,
    ) -> str:
        """
        Returns path to cached CSV. Logic:
        1. If CSV exists and covers [start_date, end_date] → return path
        2. If CSV exists but partial → fetch missing candles, append, return path
        3. If no CSV → fetch full range, write, return path
        Respects exchange rate limits (sleep between chunk fetches).
        """

    async def ensure_all_cached(
        self,
        exchange_name: str,
        pairs: list[str],
        timeframe: str,
        start_date: str,
        end_date: str,
        on_progress: Callable | None = None,
    ) -> dict[str, str]:
        """
        Caches all pairs sequentially (to respect rate limits).
        Returns {pair: csv_path} mapping.
        """
```

**CSV format** (same as existing bot format):
```csv
timestamp,open,high,low,close,volume
2025-03-18 00:00:00,84500.0,84600.0,84400.0,84550.0,1234.56
```

**Rate limit handling:** Reuses the existing `BacktestExchangeService` fetch logic with its built-in retry + backoff. Fetches pairs sequentially (not parallel) to stay within Binance's 1200 req/min limit.

### 3.4 WalkForwardEngine (`walk_forward.py`)

Generates rolling windows and computes volatility-based ranges.

```python
@dataclass(frozen=True)
class WindowRange:
    center: float
    top: float
    bottom: float
    std_dev: float

@dataclass(frozen=True)
class WalkForwardWindow:
    index: int
    train_start: str    # ISO 8601
    train_end: str
    test_start: str
    test_end: str

class WalkForwardEngine:
    def generate_windows(
        self,
        period_start: str,
        period_end: str,
        train_months: int,
        test_months: int,
    ) -> list[WalkForwardWindow]:
        """
        Rolling windows. Slide by test_months each step.
        For 1 year, train=3, test=1 → 9 windows.
        """

    def compute_volatility_range(
        self,
        ohlcv_csv_path: str,
        train_start: str,
        train_end: str,
        multiplier: float,
    ) -> WindowRange:
        """
        Load train window from CSV, compute:
          center = mean(close)
          std = std(close)
          top = center + multiplier * std
          bottom = center - multiplier * std
        """
```

**Key design:** Range is computed per (pair × window × multiplier). The `compute_volatility_range` method is pure (reads CSV, returns values) — safe to call from any process.

### 3.5 CombinationGenerator (`combination_generator.py`)

Generates all valid backtest jobs from the cartesian product.

```python
@dataclass(frozen=True)
class BacktestJob:
    """Immutable, hashable, serializable. One unit of work."""
    job_id: str                          # deterministic hash for checkpoint
    pair: str
    window: WalkForwardWindow
    # Sweep params
    strategy_type: str
    spacing: str
    num_grids: int
    range_volatility_multiplier: float
    buy_ratio: float
    sell_ratio: float
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

@dataclass
class FilterStats:
    total_generated: int
    total_valid: int
    spacing_too_tight: int              # grid spacing < 2 × fee
    range_too_narrow: int               # range can't fit num_grids levels
    tick_size_violation: int            # spacing < min tick

class CombinationGenerator:
    def __init__(self, trading_fee: float):
        self.trading_fee = trading_fee

    def generate(
        self,
        sweep_config: SweepConfig,
        pairs: list[str],
        windows: list[WalkForwardWindow],
        ohlcv_paths: dict[str, str],
        walk_forward_engine: WalkForwardEngine,
    ) -> tuple[list[BacktestJob], FilterStats]:
        """
        1. For each (pair × window × sweep_combo):
           a. Compute volatility range from train window
           b. Compute grid spacing for this range + num_grids
           c. Filter: spacing > 2 × fee? range fits num_grids?
           d. If valid → create BacktestJob
        2. Return (valid_jobs, filter_stats)
        """

    def _is_valid_combination(
        self,
        range_top: float,
        range_bottom: float,
        num_grids: int,
        spacing: str,
    ) -> tuple[bool, str]:
        """Returns (is_valid, rejection_reason)."""
```

**Job ID generation:** Deterministic hash of all job parameters. Used for checkpoint matching.

```python
def _compute_job_id(self, job: BacktestJob) -> str:
    key = f"{job.pair}|{job.window.index}|{job.strategy_type}|{job.spacing}|"
          f"{job.num_grids}|{job.range_volatility_multiplier}|"
          f"{job.buy_ratio}|{job.sell_ratio}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]
```

### 3.6 SweepExecutor (`executor.py`)

Dispatches jobs to a multiprocessing pool with progress tracking and checkpoint/resume.

```python
@dataclass
class BacktestResult:
    job: BacktestJob
    phase: Literal["train", "test"]
    roi: float
    max_drawdown: float
    sharpe_ratio: float
    sortino_ratio: float
    num_buy_trades: int
    num_sell_trades: int
    total_fees: float
    grid_trading_gains: float
    buy_and_hold_return: float
    error: str | None = None           # non-None if backtest crashed

class CheckpointManager:
    def __init__(self, checkpoint_path: str):
        self.checkpoint_path = checkpoint_path

    def load(self) -> set[str]:
        """Load set of completed job_ids."""

    def save_batch(self, completed_job_ids: set[str], results: list[dict]):
        """Atomically append completed results and update checkpoint."""

class SweepExecutor:
    def __init__(
        self,
        max_workers: int | None = None,   # None = auto-detect
        checkpoint_path: str | None = None,
        resume: bool = False,
    ):
        self.max_workers = max_workers or self._auto_detect_workers()
        self.checkpoint_manager = CheckpointManager(checkpoint_path) if checkpoint_path else None
        self.resume = resume

    def _auto_detect_workers(self) -> int:
        """
        min(cpu_count, memory_based_limit).
        memory_based_limit = (available_ram * 0.8) // estimated_per_worker_mb
        """

    def execute(
        self,
        jobs: list[BacktestJob],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[BacktestResult]:
        """
        1. If resume: load checkpoint, filter out completed jobs
        2. Register SIGINT handler for graceful shutdown
        3. Create ProcessPoolExecutor(max_workers)
        4. Submit all jobs via executor.map / as_completed
        5. Update progress bar on each completion
        6. Checkpoint every N completions (batch of 100)
        7. On SIGINT: wait for in-flight jobs, save checkpoint, return partial results
        8. Return all results (completed from checkpoint + new)
        """
```

**Why `multiprocessing` not `asyncio`:** Each backtest is CPU-bound (pandas operations on 130K rows). asyncio won't parallelize CPU work due to GIL. `ProcessPoolExecutor` gives true parallelism across M2 cores.

**Memory guard:** Each 3-month 1m backtest loads ~130K rows × ~50 bytes/row ≈ 6.5MB raw data, plus bot object graph ≈ ~50-80MB total per worker. With 16GB RAM, 80% = 12.8GB → max ~160 workers theoretically, but CPU-bound so capped at CPU count (≈10-12 effective workers on M2).

### 3.7 Worker (`worker.py`)

Top-level function that runs in a subprocess. Must be picklable (module-level function).

```python
def run_single_backtest(job: BacktestJob, phase: Literal["train", "test"]) -> BacktestResult:
    """
    Runs in a subprocess. Creates a full GridTradingBot from scratch and executes.

    Steps:
    1. Build config dict from BacktestJob fields
    2. Determine date range: job.window.train_start/end or test_start/end based on phase
    3. Set historical_data_file = job.ohlcv_csv_path
    4. Create ConfigManager.from_dict(config_dict)
    5. Create EventBus, NotificationHandler (no-op), GridTradingBot
    6. Run: asyncio.run(bot.run())
    7. Extract metrics from result["performance_summary"]
    8. Return BacktestResult
    """

def run_backtest_pair(job: BacktestJob) -> tuple[BacktestResult, BacktestResult]:
    """
    Entry point for the process pool.
    Runs train phase, then test phase, returns both results.
    """
    train_result = run_single_backtest(job, "train")
    test_result = run_single_backtest(job, "test")
    return (train_result, test_result)
```

**Config dict builder — the mapping layer (FR-2.5):**

```python
def _build_config_dict(job: BacktestJob, start_date: str, end_date: str) -> dict:
    """
    Maps BacktestJob fields to the config.json schema expected by ConfigManager.
    This is the ONLY place that knows about both schemas.
    """
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
            "type": job.strategy_type,
            "spacing": job.spacing,
            "num_grids": job.num_grids,
            "range": {"top": job.range_top, "bottom": job.range_bottom},
            "buy_ratio": job.buy_ratio,
            "sell_ratio": job.sell_ratio,
        },
        "risk_management": {
            "take_profit": {"enabled": False, "threshold": 0},
            "stop_loss": {"enabled": False, "threshold": 0},
        },
        "execution": {
            "backtest_slippage": job.backtest_slippage,
        },
        "logging": {
            "log_level": "WARNING",   # Suppress per-backtest logs in sweep mode
            "log_to_file": False,
        },
    }
```

### 3.8 SweepReporter (`reporter.py`)

Generates the output CSV and console summary.

```python
class SweepReporter:
    def write_csv(
        self,
        results: list[tuple[BacktestResult, BacktestResult]],  # (train, test) pairs
        output_path: str,
    ) -> None:
        """
        One row per (job × window). Columns as defined in FR-6.2.
        Sorted by test_roi descending.
        Uses csv.DictWriter — no pandas dependency needed.
        """

    def print_console_summary(
        self,
        results: list[tuple[BacktestResult, BacktestResult]],
        pairs: list[str],
    ) -> None:
        """
        For each pair:
          1. Group test results by config fingerprint (strategy+spacing+grids+mult+ratios)
          2. Average test_roi across all windows per config
          3. Show best config: params, avg test ROI, avg test max drawdown, win rate
        Format: tabulate table to stdout.
        """
```

---

## 4. Integration with grid_trading_bot

### 4.1 Public API Surface

`grid_sweep` depends on these `grid_trading_bot` exports. These form the **contract** between the two packages:

| Import | Usage in grid_sweep | Stability |
|--------|-------------------|-----------|
| `ConfigManager` | Create bot config from dict | Needs `from_dict()` addition |
| `ConfigValidator` | Validate generated configs | Stable |
| `GridTradingBot` | Run individual backtests | Stable (constructor + `run()`) |
| `EventBus` | Required by GridTradingBot constructor | Stable |
| `NotificationHandler` | Required by GridTradingBot constructor | Stable |
| `TradingMode` | Set backtest mode | Stable (enum) |

### 4.2 ConfigManager Modification (only change to grid_trading_bot)

Add a `from_dict` classmethod so configs can be created programmatically without temp JSON files. This is a general-purpose improvement useful beyond sweep.

```python
# In grid_trading_bot/config/config_manager.py — add classmethod

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

### 4.3 GridTradingBot — No Changes

The worker creates `GridTradingBot` the same way `run_bot()` does today. The bot doesn't know it's being used by grid_sweep. The only difference:
- `config_path` is `"<sweep>"` instead of a real file path
- `no_plot` is always `True`
- `NotificationHandler` gets empty notification URLs (no alerts during sweep)

### 4.4 grid_trading_bot CLI — No Changes

The bot's CLI (`grid_trading_bot run`) is **untouched**. The sweep has its own CLI entry point (`grid_sweep`). No subcommand registration needed.

---

## 5. Data Flow

```
                                    ┌─────────────────┐
                                    │   sweep.yaml     │
                                    └────────┬────────┘
                                             │
                                     SweepConfig.from_yaml()
                                             │
                              ┌──────────────┼──────────────┐
                              ▼              ▼              ▼
                        PairResolver    OHLCVCache    WalkForwardEngine
                        (resolve       (fetch &       (generate 9
                         top 10)        cache CSVs)    windows)
                              │              │              │
                              └──────────────┼──────────────┘
                                             │
                                   CombinationGenerator
                                   (720 combos × 10 pairs × 9 windows)
                                   (filter invalid → ~N valid jobs)
                                             │
                                             ▼
                                      SweepExecutor
                              ┌──────────────┼──────────────┐
                              ▼              ▼              ▼
                          Worker 1       Worker 2  ...  Worker 12
                          (subprocess)   (subprocess)   (subprocess)
                              │              │              │
                              │    ┌─────────┴─────────┐   │
                              │    │  GridTradingBot    │   │
                              │    │  (existing code)   │   │
                              │    │  train → test      │   │
                              │    └─────────┬─────────┘   │
                              │              │              │
                              └──────────────┼──────────────┘
                                             │
                                      BacktestResult[]
                                             │
                                       SweepReporter
                                     ┌───────┴───────┐
                                     ▼               ▼
                              CSV file         Console summary
                         (sorted by ROI)    (best config per pair)
```

---

## 6. Checkpoint & Resume Design

```
results/
├── sweep_20260318_143022.csv          ← final output
└── .sweep_checkpoint_20260318_143022.json  ← checkpoint file
```

**Checkpoint file structure:**

```json
{
    "sweep_yaml_hash": "a1b2c3...",
    "completed_job_ids": ["job_abc123", "job_def456", ...],
    "results": [
        {
            "job_id": "job_abc123",
            "pair": "BTC/USDT",
            "window_index": 0,
            "train": { "roi": -1.5, "max_drawdown": 3.2, ... },
            "test": { "roi": 0.8, "max_drawdown": 2.1, ... }
        },
        ...
    ]
}
```

**Resume logic:**
1. Hash the sweep YAML content
2. If checkpoint exists AND yaml hash matches → load completed job IDs
3. Filter `jobs` list to exclude already-completed ones
4. Run remaining jobs
5. Merge new results with checkpoint results
6. Write final CSV from merged results

**Graceful interruption (SIGINT):**
1. Set `shutdown_requested = True` flag
2. Let in-flight workers finish (don't cancel them — partial bot runs produce no result)
3. Save checkpoint with all completed results
4. Print "Interrupted. X/Y jobs completed. Resume with --resume"

---

## 7. Sequence Diagram — Full Sweep Execution

```
User            grid_sweep CLI       SweepConfig    PairResolver    OHLCVCache
 │                   │                   │              │              │
 │  grid_sweep run   │                   │              │              │
 │  --config s.yaml  │                   │              │              │
 │──────────────────>│                   │              │              │
 │                   │  from_yaml()      │              │              │
 │                   │──────────────────>│              │              │
 │                   │  <SweepConfig>    │              │              │
 │                   │<──────────────────│              │              │
 │                   │                                  │              │
 │                   │  resolve(auto, 10)               │              │
 │                   │─────────────────────────────────>│              │
 │                   │  ["BTC/USDT", "ETH/USDT", ...]  │              │
 │                   │<─────────────────────────────────│              │
 │                   │                                                 │
 │                   │  ensure_all_cached(10 pairs, 1yr, 1m)          │
 │                   │────────────────────────────────────────────────>│
 │                   │  {pair: csv_path} mapping                      │
 │                   │<────────────────────────────────────────────────│

                  WalkForward    CombinationGen    Executor       Reporter
                      │              │                │              │
 │                   │              │                │              │
 │                   │  generate_windows(9)          │              │
 │                   │─>│                            │              │
 │                   │  │ [Window0..Window8]         │              │
 │                   │<─│                            │              │
 │                   │                               │              │
 │                   │  generate(combos × pairs × windows)          │
 │                   │──────────────>│               │              │
 │                   │  (jobs[], filter_stats)       │              │
 │                   │<──────────────│               │              │
 │                   │                               │              │
 │  "64,800 jobs,    │                               │              │
 │   1,230 filtered" │                               │              │
 │<──────────────────│                               │              │
 │                   │                               │              │
 │                   │  execute(jobs)                │              │
 │                   │──────────────────────────────>│              │
 │                   │              ┌────────────────┤              │
 │  [progress bar]   │              │  12 workers    │              │
 │<─ ─ ─ ─ ─ ─ ─ ─ ─│              │  train + test  │              │
 │                   │              │  per job       │              │
 │                   │              └────────────────┤              │
 │                   │  results[]                    │              │
 │                   │<──────────────────────────────│              │
 │                   │                                              │
 │                   │  write_csv() + print_summary()               │
 │                   │─────────────────────────────────────────────>│
 │  "Best config     │                                              │
 │   per pair:"      │                                              │
 │<──────────────────│                                              │
```

---

## 8. CLI Interface Design

The sweep has its own independent CLI entry point, registered via `pyproject.toml` console script.

```
# Usage
grid_sweep run --config sweep.yaml
grid_sweep run --config sweep.yaml --dry-run
grid_sweep run --config sweep.yaml --fetch-only
grid_sweep run --config sweep.yaml --workers 8 --resume
```

```python
# grid_sweep/cli.py

@click.group()
@click.version_option(package_name="grid_sweep")
def main():
    """Grid Sweep — Parameter sweep with walk-forward validation."""

@main.command()
@click.option("--config", required=True, type=click.Path(exists=True), help="Path to sweep YAML file")
@click.option("--output", default=None, type=click.Path(), help="Output CSV path")
@click.option("--workers", default=None, type=int, help="Parallel workers (default: auto)")
@click.option("--resume", is_flag=True, help="Resume from checkpoint")
@click.option("--dry-run", is_flag=True, help="Show job count and estimated time")
@click.option("--fetch-only", is_flag=True, help="Only fetch and cache data")
@click.option("--pairs-only", is_flag=True, help="Only list resolved pairs")
def run(config, output, workers, resume, dry_run, fetch_only, pairs_only):
    """Run a parameter sweep with walk-forward validation."""
    asyncio.run(_sweep_main(config, output, workers, resume, dry_run, fetch_only, pairs_only))
```

**`--dry-run` output example:**

```
Sweep configuration:
  Exchange:     binance
  Pairs:        10 (auto, top by USDT volume)
  Period:       2025-03-18 → 2026-03-18 (12 months)
  Walk-forward: train=3mo, test=1mo, slide=1mo → 9 windows
  Timeframe:    1m

Parameter sweep:
  strategy_type:              2 values  [simple_grid, hedged_grid]
  spacing:                    2 values  [arithmetic, geometric]
  num_grids:                  5 values  [4, 8, 12, 16, 20]
  range_volatility_multiplier: 4 values  [1.0, 1.5, 2.0, 2.5]
  buy_ratio:                  3 values  [0.5, 0.75, 1.0]
  sell_ratio:                 3 values  [0.5, 0.75, 1.0]

Total: 720 combinations × 10 pairs × 9 windows = 64,800 jobs
After filtering: ~63,570 valid jobs (estimated)
Each job = 1 train run + 1 test run = ~127,140 bot executions

Workers:     12 (auto-detected, M2 16GB)
Est. time:   ~6-8 hours
Output:      results/sweep_20260318_143022.csv
```

---

## 9. grid_sweep pyproject.toml

```toml
[project]
name = "grid_sweep"
version = "0.1.0"
description = "Parameter sweep with walk-forward validation for grid_trading_bot"
requires-python = ">=3.12"
dependencies = [
    "grid_trading_bot @ file:../grid_trading_bot",   # local monorepo dep
    "pyyaml>=6.0",                                    # sweep YAML parsing
    "tqdm>=4.60",                                     # progress bar
    "click>=8.1",                                     # CLI (also a dep of grid_trading_bot)
    "tabulate>=0.9",                                  # console summary tables
]

[project.scripts]
grid_sweep = "grid_sweep.cli:main"

[project.optional-dependencies]
dev = [
    "pytest>=9.0",
    "pytest-asyncio>=1.3",
    "ruff>=0.8",
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

[tool.ruff]
line-length = 120
target-version = "py312"
```

**No new heavy dependencies.** `ccxt`, `pandas`, `numpy` come transitively via `grid_trading_bot`.

### Install workflow

```bash
# From algo_trading/ root — install both in dev mode
cd grid_trading_bot && uv sync --all-extras --dev && cd ..
cd grid_sweep && uv sync --dev && cd ..

# Or install grid_sweep only (pulls grid_trading_bot automatically)
cd grid_sweep && uv sync --dev
```

---

## 10. Error Handling Strategy

| Scenario | Handling |
|----------|----------|
| Single backtest crashes | Log error with job details, record `error` field in result, continue with remaining jobs |
| OHLCV fetch fails for one pair | Retry 3× with backoff (existing logic). If still fails, skip pair, log warning, continue |
| All workers crash (OOM) | Detect via ProcessPoolExecutor, reduce workers by half, retry. If still fails, abort with message |
| Invalid sweep YAML | Fail fast with clear validation error before any fetching/execution |
| Checkpoint file corrupted | Log warning, start from scratch (don't crash) |
| Disk full during CSV write | Catch IOError, print partial results to console as fallback |

---

## 11. Performance Considerations

| Aspect | Design Decision | Rationale |
|--------|----------------|-----------|
| Data loading | Each worker reads CSV from disk | CSV files are cached locally; OS page cache makes repeated reads fast. Avoids complex shared memory for DataFrames |
| Worker isolation | Each subprocess creates its own bot stack | No shared state = no locks = no deadlocks. Memory overhead is acceptable at ~50-80MB/worker |
| Logging | Workers use `WARNING` level | Prevents log spam from 65K backtests. Errors still captured |
| Checkpoint frequency | Every 100 completed jobs | Balances write overhead vs data loss risk |
| Progress bar | Updated per job completion | `tqdm` with `multiprocessing`-compatible callback |
| Train phase optimization | Future: skip train run if only test metrics matter for ranking | For v1, run both train+test (train ROI is useful reference data) |

---

## 12. File Inventory — What Gets Created/Modified

### grid_trading_bot (existing repo — minimal changes)

| File | Action | Description |
|------|--------|-------------|
| `src/grid_trading_bot/config/config_manager.py` | **MODIFY** | Add `from_dict()` classmethod (~10 lines) |

### grid_sweep (new project)

| File | Action | Description |
|------|--------|-------------|
| `pyproject.toml` | **CREATE** | Project config with grid_trading_bot dependency |
| `CLAUDE.md` | **CREATE** | Development instructions for grid_sweep |
| `src/grid_sweep/__init__.py` | **CREATE** | Package init |
| `src/grid_sweep/cli.py` | **CREATE** | Click CLI entry point: `grid_sweep` |
| `src/grid_sweep/sweep_config.py` | **CREATE** | YAML parsing + SweepConfig dataclass |
| `src/grid_sweep/pair_resolver.py` | **CREATE** | Top-N pair resolution via CCXT |
| `src/grid_sweep/ohlcv_cache.py` | **CREATE** | Data fetch + CSV cache management |
| `src/grid_sweep/walk_forward.py` | **CREATE** | Window generation + volatility calc |
| `src/grid_sweep/combination_generator.py` | **CREATE** | Cartesian product + filter |
| `src/grid_sweep/executor.py` | **CREATE** | Multiprocessing orchestration + checkpoint |
| `src/grid_sweep/worker.py` | **CREATE** | Single backtest runner for subprocesses |
| `src/grid_sweep/reporter.py` | **CREATE** | CSV output + console summary |
| `config/sweep.yaml` | **CREATE** | Example sweep configuration |
| `tests/` | **CREATE** | Test suite for grid_sweep |
