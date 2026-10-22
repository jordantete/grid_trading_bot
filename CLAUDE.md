# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A cryptocurrency grid trading bot supporting backtesting, paper trading, and live trading. Uses CCXT for exchange integration, with an event-driven architecture and strategy pattern for extensibility. Python 3.12+, managed with `uv`. Packaged as a src layout (`src/grid_trading_bot/`) and installable via `pip install grid_trading_bot`.

## Common Commands

```bash
# Install dependencies
uv sync --all-extras --dev

# Run the bot (backtest mode by default via config)
uv run grid_trading_bot run --config config/config.json

# Alternative: run via python -m
uv run python -m grid_trading_bot run --config config/config.json

# Show version
uv run grid_trading_bot --version

# Run all tests with coverage (unit + integration, excludes sandbox)
uv run python -m pytest --cov=grid_trading_bot --cov-report=term

# Run a single test file
uv run python -m pytest tests/order_handling/test_order_manager.py

# Run a single test
uv run python -m pytest tests/order_handling/test_order_manager.py::TestClassName::test_method_name

# Integration tests (backtest E2E, no network needed)
uv run python -m pytest tests/integration/test_backtest_e2e.py -v

# Sandbox smoke tests (requires network, validates ccxt and its aiohttp transport)
uv run python -m pytest -m sandbox -v

# Update snapshots after intentional behavior changes
uv run python -m pytest tests/integration/test_backtest_e2e.py --update-snapshots

# Lint and format (via pre-commit or directly)
uv run ruff check --fix .
uv run ruff format .

# Type check (src/ only, must report 0 errors)
uv run mypy

# Run pre-commit hooks
uv run pre-commit run --all-files

# Start monitoring stack (Grafana/Loki/Alloy)
docker-compose up -d
```

## Architecture

### Package Layout

The project uses a **src layout**: all source code lives under `src/grid_trading_bot/`. Tests stay at the repo root under `tests/`. Runtime config files are in `config/`.

### Entry Point

`cli.py` is the Click-based CLI entry point (registered as `grid_trading_bot` console script). It parses CLI args via the `run` subcommand, creates a `GridTradingBot` per config file, and runs them concurrently via `asyncio.gather`. In live/paper mode, `BotController` (CLI commands) and `HealthCheck` (system monitoring) run as concurrent tasks. Supports `python -m grid_trading_bot` via `__main__.py`.

### Core Modules

- **`core/bot_management/`** — Bot lifecycle: `GridTradingBot` (orchestrator), `EventBus` (pub/sub, `STOP_BOT` only), `BotController` (CLI), `HealthCheck`, `NotificationHandler` (Apprise alerts)
- **`core/grid_management/`** — `GridManager` computes grid levels (arithmetic/geometric spacing), `GridLevel` waits in its home (free) state, moves to `WAITING_FOR_*_FILL` while it holds orders, and returns home when the last one fills or is cancelled; each `GridStrategy` plans the initial orders and picks the level of every paired order
- **`core/order_handling/`** — `OrderManager` orchestrates order placement and pairing (buy fill → place sell above, sell fill → place buy below) through one placement helper and a `PlacementJournal` (each placement written to the database before it is sent, then retried at every poll until it is placed or refused). `BalanceTracker` tracks fiat/crypto with reserved amounts and books fees in the currency received. `OrderBook` maps orders to grid levels
- **`core/persistence/`** — SQLite state persistence (live and paper trading): `StatePersistenceService` (one-transaction snapshots saved by `OrderManager` after every order operation; alerts, then halts after 3 failed writes in a row), `StateRecoveryService` (resumes, refuses or defers a start; never deletes the database), the `pending_placements` journal (schema v4), `SQLiteStateRepository` (WAL-mode SQLite backend), `Serializers` (Order/GridLevel/Balance ↔ dict). DB path: `data/{BASE}_{QUOTE}/state_{config_hash[:8]}.db`
- **`core/services/`** — Exchange abstraction via `ExchangeInterface` ABC. `BacktestExchangeService` reads CSV/CCXT OHLCV data. `LiveExchangeService` uses CCXT Pro WebSockets
- **`core/order_handling/execution_strategy/`** — `OrderExecutionStrategyInterface` ABC. Backtest variant is instant; live variant sends a `clientOrderId` with every order and, after a lost answer, looks the order up by that id before reporting it placed, not placed or unknown
- **`core/validation/`** — `OrderValidator` for order quantity/price validation

### Strategy Layer

- **`strategies/grid_trading_strategy.py`** — Implements `TradingStrategyInterface`. Handles both backtest (iterate OHLCV rows) and live (WebSocket price stream) execution loops. Manages the initial purchase (sized to fund the sell orders above the price), grid order initialization, take-profit/stop-loss
- **`strategies/trading_performance_analyzer.py`** — Computes ROI, max drawdown, Sharpe ratio, etc.
- **`strategies/plotter.py`** — Plotly visualization for backtest results

### Config & Utils

- **`config/`** — `ConfigManager` (accessor methods for nested JSON), `ConfigValidator` (schema validation), `TradingMode` enum, custom `ConfigError` exceptions
- **`utils/`** — Logging setup, config name generation, performance results I/O

### Key Design Patterns

- **Factory Pattern**: `ExchangeServiceFactory` and `OrderExecutionStrategyFactory` select implementations based on `TradingMode`
- **Event Bus**: carries `STOP_BOT` only. Order updates go through `OrderManager.apply_order_update` (balance, level, paired order, save, in that order), and every placement through `OrderManager._submit_limit` and the `PlacementJournal`
- **State Machine**: `GridLevel` transitions through `GridCycleState` enum states as orders are placed and filled
- **Strategy Pattern**: `TradingStrategyInterface` ABC; currently one implementation (`GridTradingStrategy`) with two modes (`simple_grid`, `hedged_grid`)

### Trading Modes

Configured via `exchange.trading_mode` in `config.json`:
- **`backtest`**: Simulates on historical OHLCV data (CSV or fetched via CCXT). No real orders
- **`paper_trading`**: Connects to exchange sandbox APIs with real market data. Runs BotController + HealthCheck
- **`live`**: Real trading with actual funds. Same architecture as paper trading but against production APIs. Includes SQLite state persistence for crash recovery (a checkpoint after every order operation, merge recovery on restart); paper trading persists and resumes its state the same way

## Testing

Tests mirror the source structure under `tests/`. Async tests use `pytest-asyncio` with `asyncio_mode = "auto"`. The `conftest.py` provides a `valid_config` fixture with a complete config dict. Coverage excludes interface files (`*/interface*.py`).

All imports use the full package path: `from grid_trading_bot.config.X import Y`. The `pythonpath = ["src"]` setting in `pyproject.toml` allows pytest to resolve these imports.

### Unit Tests

Located in `tests/` (excluding `tests/integration/`). Heavy use of mocking to isolate components.

### Integration Tests

Located in `tests/integration/`. **Zero mocks** — the full bot stack runs against real CSV data.

- **`test_backtest_e2e.py`** — 16 parametrized tests covering 4 scenarios (simple_grid/hedged_grid x arithmetic/geometric). Validates: no crash, balance coherence, grid cycle integrity, and deterministic results via snapshot comparison.
- **`test_sandbox_smoke.py`** — 3 tests that make real network calls to Binance public API (REST + WebSocket). Validates ccxt compatibility, including the aiohttp transport it brings. Marked `@pytest.mark.sandbox` and excluded from default runs.
- **`snapshots/`** — JSON reference files: every `PerformanceMetrics` field at full precision, the final balances and the fill list. A missing snapshot fails the test. After an intentional behavior change, `--update-snapshots` rewrites them, the tests pass, and the final summary lists every field and fill that changed.

### Pytest Markers

- `integration` — E2E backtest tests
- `sandbox` — Tests requiring network access (excluded by default via `addopts`)

## Documentation

The project uses [Zensical](https://github.com/nicholasgriffintn/zensical) (Material for MkDocs successor) for web documentation, hosted on GitHub Pages.

- **Config**: `zensical.toml` at repo root
- **Source files**: `docs/` directory (Markdown + Mermaid diagrams)
- **Build output**: `site/` (gitignored)

```bash
# Preview locally
uv sync --extra docs && uv run zensical serve

# Build static site
uv run zensical build --clean
```

Deploy is automatic via GitHub Actions (`.github/workflows/docs.yml`) on push to `master`.

## Linting & Formatting

Ruff is configured in `pyproject.toml` with 120-char line length, targeting Python 3.12. mypy is configured there too, over `src/` only, and must report 0 errors. Pre-commit hooks run Ruff lint+format and mypy with the versions pinned in `pyproject.toml` (they run through `uv run --frozen --no-sync`, so they need an existing `.venv`: run `uv sync --all-extras --dev` first), plus checks for trailing whitespace, valid YAML/JSON/TOML, merge conflicts, debug statements, blanket `noqa`/`type: ignore`, and deprecated `log.warn()`. A `# type: ignore` carries its error code and a one-line reason.

CI (`.github/workflows/ci.yml`) runs on every push to `master` and every pull request: pytest on Python 3.12 and 3.14, and a `lint` job (`ruff check`, `ruff format --check`, `mypy`). Releases follow "Releasing" in `docs/contributing/guide.md`: a `v*` tag must match the `pyproject.toml` version and sit on `master`, and publishing to PyPI waits for approval of the `pypi` environment.

## Behavioral Guidelines (Karpathy/Multica)

These four principles bias toward caution and clarity over raw speed — most valuable on complex or ambiguous tasks, with free judgment on trivial ones. Source: [multica-ai/andrej-karpathy-skills](https://github.com/multica-ai/andrej-karpathy-skills/blob/main/CLAUDE.md). **The project-specific rules above always take precedence over anything here; on any conflict, the project rule wins.**

1. **Think Before Coding** — Don't assume, don't hide confusion, surface tradeoffs. State assumptions explicitly; if a request has several valid interpretations, present them instead of silently picking one; if a simpler approach exists, say so; if something is unclear, stop and ask rather than guessing.
2. **Simplicity First** — Write the minimum code that solves the problem, nothing speculative. No unrequested features, no abstractions for a single use, no error handling for edge cases that can't occur. If a change balloons (e.g. 200 lines where 50 would do), step back and rewrite.
3. **Surgical Changes** — Touch only what the task requires. Don't "improve" adjacent code, and match the existing style. Flag pre-existing dead code rather than deleting it; clean up only the imports/functions your own changes made obsolete.
4. **Goal-Driven Execution** — Turn vague requests into verifiable success criteria, then loop until they hold. "Fix the bug" becomes "write a failing test that reproduces it, then make it pass." Sketch a brief multi-step plan and verify each step. For this repo, the default success bar is `uv run python -m pytest --cov=grid_trading_bot` green and `uv run ruff check .` / `uv run ruff format .` / `uv run mypy` clean.
