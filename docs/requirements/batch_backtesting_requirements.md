# Batch Backtesting — Requirements Specification

## 1. Goal

Provide a single CLI command that:
1. Fetches and caches historical data for multiple pairs
2. Generates all parameter combinations from a sweep definition
3. Runs walk-forward backtests across all combinations and pairs
4. Produces a CSV report ranked by performance, identifying the best configurations

The primary use case is finding a robust grid trading configuration before live deployment, while minimizing overfitting risk.

---

## 2. Functional Requirements

### FR-1: Data Fetching & Caching

| ID | Requirement |
|----|-------------|
| FR-1.1 | Fetch OHLCV data via CCXT for a configurable list of trading pairs |
| FR-1.2 | Default pair selection: top 10 USDT pairs by 24h volume on Binance |
| FR-1.3 | Support an explicit pair list override in the sweep config |
| FR-1.4 | Cache fetched data as local CSV files (one file per pair/timeframe) |
| FR-1.5 | Cache location: `data/ohlcv_cache/{exchange}/{pair}_{timeframe}.csv` |
| FR-1.6 | Skip re-fetching if cached file already covers the requested date range |
| FR-1.7 | Support incremental cache update (append missing candles to existing file) |
| FR-1.8 | Default timeframe: 1m. Configurable in sweep definition |
| FR-1.9 | Default period: 1 year. Configurable via start_date/end_date |

### FR-2: Sweep Definition

| ID | Requirement |
|----|-------------|
| FR-2.1 | Sweep parameters defined in a single YAML file |
| FR-2.2 | Each parameter accepts either a fixed value or a list of values to sweep |
| FR-2.3 | Generate the full cartesian product of all parameter lists |
| FR-2.4 | Support the following sweep parameters (initial set): |

**Sweep parameters (v1):**

| Parameter | Type | Example values |
|-----------|------|----------------|
| `strategy_type` | enum | `[simple_grid, hedged_grid]` |
| `spacing` | enum | `[arithmetic, geometric]` |
| `num_grids` | int list | `[4, 8, 12, 16, 20]` |
| `range_volatility_multiplier` | float list | `[1.0, 1.5, 2.0, 2.5]` |
| `buy_ratio` | float list | `[0.5, 0.75, 1.0]` |
| `sell_ratio` | float list | `[0.5, 0.75, 1.0]` |

| ID | Requirement (continued) |
|----|-------------|
| FR-2.5 | The sweep file must be extensible — adding a new parameter with a list of values should automatically include it in the cartesian product without code changes |
| FR-2.6 | Fixed parameters (trading_fee, initial_balance, exchange, etc.) are defined once at the top level of the sweep file |
| FR-2.7 | Log total number of combinations before starting (e.g., "720 configs × 10 pairs × 10 windows = 72,000 backtests") |

**Example sweep definition:**

```yaml
# sweep.yaml
exchange: binance
trading_fee: 0.001
initial_balance: 1000
timeframe: 1m
backtest_slippage: 0.0

# Data
period:
  start_date: "2025-03-18T00:00:00Z"  # 1 year ago from today
  end_date: "2026-03-18T00:00:00Z"

pairs:
  mode: auto            # "auto" = top N by volume, "manual" = explicit list
  count: 10             # used when mode=auto
  quote_currency: USDT
  # manual_list:        # used when mode=manual
  #   - BTC/USDT
  #   - ETH/USDT

# Walk-forward
walk_forward:
  train_months: 3
  test_months: 1
  # Rolling: train on 3 months, test on next 1 month, slide by 1 month

# Parameter sweep (lists = sweep, scalars = fixed)
sweep:
  strategy_type: [simple_grid, hedged_grid]
  spacing: [arithmetic, geometric]
  num_grids: [4, 8, 12, 16, 20]
  range_volatility_multiplier: [1.0, 1.5, 2.0, 2.5]
  buy_ratio: [0.5, 0.75, 1.0]
  sell_ratio: [0.5, 0.75, 1.0]
```

### FR-3: Volatility-Based Range Calculation

| ID | Requirement |
|----|-------------|
| FR-3.1 | Compute the range (top/bottom) dynamically per pair per walk-forward window |
| FR-3.2 | Calculate the standard deviation of close prices over the **training** window |
| FR-3.3 | `range_center` = mean close price of the training window |
| FR-3.4 | `range_top` = `range_center` + (`range_volatility_multiplier` × std_dev) |
| FR-3.5 | `range_bottom` = `range_center` - (`range_volatility_multiplier` × std_dev) |
| FR-3.6 | This replaces the static `range.top` / `range.bottom` from config.json |
| FR-3.7 | The multiplier is a sweep parameter, allowing comparison of tight vs wide grids |

### FR-4: Walk-Forward Execution

| ID | Requirement |
|----|-------------|
| FR-4.1 | Split the full period into rolling windows based on `train_months` and `test_months` |
| FR-4.2 | Window generation: start at period.start_date, create train window of `train_months`, followed by test window of `test_months`, then slide forward by `test_months` and repeat |
| FR-4.3 | Example for 1 year with train=3mo, test=1mo: 9 windows (months 0-3→3-4, 1-4→4-5, ..., 8-11→11-12) |
| FR-4.4 | For each parameter combination: run backtest on train window, then run the **same config** on the test window |
| FR-4.5 | Grid range (top/bottom) is calculated from the **train** window data (FR-3), then applied to **both** train and test runs |
| FR-4.6 | Record metrics separately for train and test phases |
| FR-4.7 | The final ranking uses **aggregated test-window metrics only** (not train metrics) — this is the anti-overfitting mechanism |

### FR-5: Parallel Execution

| ID | Requirement |
|----|-------------|
| FR-5.1 | Run backtests in parallel using a worker pool |
| FR-5.2 | Default workers = number of CPU cores (auto-detect). Configurable via `--workers N` |
| FR-5.3 | Use `multiprocessing` (not asyncio) to bypass GIL and utilize all cores |
| FR-5.4 | Memory guard: estimate memory per backtest (~50-100MB for 1m data over 3 months), limit concurrent workers to stay under 80% of available RAM |
| FR-5.5 | Display a progress bar (tqdm or similar) showing: completed/total backtests, elapsed time, ETA |
| FR-5.6 | On interruption (SIGINT/Ctrl+C): gracefully stop, save all completed results to output file |
| FR-5.7 | Checkpoint/resume: save progress to a checkpoint file. On re-run with same sweep file, skip already-completed combinations |
| FR-5.8 | Checkpoint file location: alongside the output CSV (e.g., `results/.sweep_checkpoint.json`) |

### FR-6: Output & Reporting

| ID | Requirement |
|----|-------------|
| FR-6.1 | Primary output: a single CSV file with one row per (combination × pair × window) |
| FR-6.2 | CSV columns (at minimum): |

**CSV columns:**

| Column | Description |
|--------|-------------|
| `pair` | Trading pair (e.g., SOL/USDT) |
| `window_start` | Test window start date |
| `window_end` | Test window end date |
| `strategy_type` | simple_grid or hedged_grid |
| `spacing` | arithmetic or geometric |
| `num_grids` | Number of grid levels |
| `range_volatility_multiplier` | Volatility multiplier used |
| `range_top` | Computed range top price |
| `range_bottom` | Computed range bottom price |
| `buy_ratio` | Buy ratio used |
| `sell_ratio` | Sell ratio used |
| `train_roi` | ROI on training window (for reference) |
| `test_roi` | ROI on test window (primary metric) |
| `train_max_drawdown` | Max drawdown on training window |
| `test_max_drawdown` | Max drawdown on test window (primary metric) |
| `test_sharpe_ratio` | Sharpe ratio on test window |
| `test_sortino_ratio` | Sortino ratio on test window |
| `test_num_buy_trades` | Buy trades in test window |
| `test_num_sell_trades` | Sell trades in test window |
| `test_total_fees` | Total fees in test window |
| `test_grid_trading_gains` | Grid P&L in test window |
| `test_buy_and_hold_return` | Buy & hold comparison |

| ID | Requirement (continued) |
|----|-------------|
| FR-6.3 | Sort CSV by `test_roi` descending by default |
| FR-6.4 | On completion, print a console summary: top 10 configs by average test ROI across all pairs and windows |
| FR-6.5 | Console summary includes: rank, strategy_type, spacing, num_grids, multiplier, ratios, avg test ROI, avg test max drawdown, number of windows where ROI > 0 |
| FR-6.6 | Default output path: `results/sweep_{timestamp}.csv` |
| FR-6.7 | Configurable via `--output path/to/file.csv` |

### FR-7: CLI Interface

| ID | Requirement |
|----|-------------|
| FR-7.1 | Separate CLI tool: `grid_sweep run --config sweep.yaml` (independent package, depends on `grid_trading_bot`) |
| FR-7.2 | Options: |

```
grid_sweep run [OPTIONS]

Options:
  --config PATH          Path to sweep YAML file (required)
  --output PATH          Output CSV path (default: results/sweep_{timestamp}.csv)
  --workers N            Number of parallel workers (default: auto)
  --resume               Resume from checkpoint if available
  --dry-run              Show number of combinations and estimated time, don't execute
  --fetch-only           Only fetch and cache data, don't run backtests
  --pairs-only           Only list pairs that would be selected, don't execute
```

| ID | Requirement (continued) |
|----|-------------|
| FR-7.3 | `--dry-run` prints: total combinations, total backtests (combos × pairs × windows), estimated duration based on a single-backtest benchmark |
| FR-7.4 | `--fetch-only` fetches and caches all OHLCV data, then exits. Useful to pre-download before a long sweep |
| FR-7.5 | Validate sweep YAML before starting: check all required fields, validate parameter ranges, verify exchange/pair validity |

---

## 3. Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR-1 | Memory usage must stay under 80% of system RAM (hard limit with worker throttling) |
| NFR-2 | Each backtest process should be isolated (no shared mutable state between workers) |
| NFR-3 | Graceful degradation: if one backtest crashes, log the error and continue with remaining |
| NFR-4 | Progress must be visible at all times (progress bar with ETA) |
| NFR-5 | Target performance: a single 3-month 1m backtest should complete in < 10 seconds on M2 |
| NFR-6 | The sweep engine must be decoupled from the grid strategy — it orchestrates existing bot infrastructure, not reimplements it |
| NFR-7 | OHLCV cache files must be reusable across sweep runs (stable file format, no sweep-specific metadata) |
| NFR-8 | Sweep YAML schema must be extensible: adding a new sweep parameter should not require changes to the sweep engine core, only to config-to-bot-config mapping |

---

## 4. User Stories

| ID | Story | Acceptance Criteria |
|----|-------|-------------------|
| US-1 | As a trader, I want to fetch and cache historical data for multiple pairs so I don't re-download data between sweeps | Data cached as CSV, re-run skips existing data, incremental update works |
| US-2 | As a trader, I want to define parameter ranges in a YAML file so I can easily modify and re-run sweeps | Changing a value in YAML changes the sweep without code modifications |
| US-3 | As a trader, I want walk-forward validation so my results reflect out-of-sample performance | Test-window metrics are separated from train metrics, ranking uses test only |
| US-4 | As a trader, I want volatility-based ranges so my grid adapts to each pair's price behavior | Range computed from std dev, different multipliers compared |
| US-5 | As a trader, I want parallel execution so sweeps complete in hours not days | All CPU cores utilized, progress bar shows ETA |
| US-6 | As a trader, I want to resume interrupted sweeps so I don't lose hours of computation | Ctrl+C saves progress, `--resume` skips completed work |
| US-7 | As a trader, I want a CSV output ranked by ROI so I can quickly identify the best configs | CSV sorted by test_roi, console shows top 10 |
| US-8 | As a trader, I want a dry-run mode so I can estimate sweep duration before committing | `--dry-run` shows combo count and time estimate |

---

## 5. Constraints & Assumptions

| # | Item |
|---|------|
| C-1 | Exchange: Binance (hardcoded default, extensible later) |
| C-2 | Quote currency: USDT only for v1 |
| C-3 | Python 3.12+, uses existing project dependencies (ccxt, pandas, click) |
| C-4 | Separate package (`grid_sweep`) in monorepo, depends on `grid_trading_bot` as a library |
| C-5 | Must reuse existing `GridTradingBot` for actual backtest execution — no reimplementation of trading logic |
| C-6 | Target machine: MacBook Pro M2, 16GB RAM, 16 cores. Must also work on Linux VPS |
| A-1 | Binance public API has rate limits (~1200 req/min); data fetching must respect this |
| A-2 | 1m candles for 1 year ≈ 525,600 rows per pair; ~50MB CSV per pair |
| A-3 | Each 3-month 1m backtest loads ~130,000 candles into memory |

---

## 6. Resolved Questions

| # | Decision |
|---|----------|
| OQ-1 | `initial_balance` fixed at 1000 USDT for all pairs (ensures comparability) |
| OQ-2 | Console summary shows **best config per pair** (not averaged across pairs) |
| OQ-3 | **Filter out invalid combinations** before execution (e.g., num_grids=20 with ±1 std range where grid spacing < min tick size) |
| OQ-4 | Walk-forward slides by `test_months` (1 month) — confirmed. 9 windows for 1 year with train=3/test=1 |

### FR-8: Combination Filtering

| ID | Requirement |
|----|-------------|
| FR-8.1 | Before execution, validate each (combination × pair × window) and discard invalid ones |
| FR-8.2 | Filter rule: grid spacing must be ≥ exchange minimum tick size for the pair |
| FR-8.3 | Filter rule: grid spacing must be > 2× trading fee to ensure theoretical profitability |
| FR-8.4 | Filter rule: range must contain at least `num_grids` feasible price levels |
| FR-8.5 | Log number of filtered-out combinations and reasons (e.g., "Filtered 1,230/64,800: 890 spacing too tight, 340 range too narrow") |

### FR-6.4 (updated): Console Summary

| ID | Requirement |
|----|-------------|
| FR-6.4 | On completion, print a summary showing **best config per pair**: for each pair, show the config with the highest average test ROI across windows, along with avg test ROI, avg test max drawdown, and win rate (windows with ROI > 0) |

---

## 7. Estimation

**With default sweep config (v1):**
- 720 parameter combinations × 10 pairs × 9 walk-forward windows = **64,800 backtests**
- Each backtest has a train run + test run = **129,600 bot executions**
- At ~5s per execution on M2 with 12 effective workers: **~15 hours**
- With optimizations (shared data loading, lighter execution): target **4-8 hours**

---

## 8. Future Considerations (Out of Scope for v1)

| Item | Description |
|------|-------------|
| Market regime detection | Classify periods (bull/bear/range) and adapt strategy per regime |
| Distributed execution | Spread sweep across multiple machines/VPS |
| Bayesian optimization | Replace brute-force grid search with smarter parameter exploration |
| Live config deployment | Auto-generate live config.json from winning sweep result |
| Result visualization | Optional heatmaps, parameter sensitivity plots |
