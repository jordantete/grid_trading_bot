# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A cryptocurrency grid trading bot supporting backtesting, paper trading, and live trading. Uses CCXT for exchange integration, with an event-driven architecture and strategy pattern for extensibility. Python 3.12+, managed with `uv`.

## Common Commands

```bash
# Install dependencies
uv sync --all-extras --dev

# Run the bot (backtest mode by default via config)
uv run python main.py --config config/config.json

# Run all tests with coverage (unit + integration, excludes sandbox)
uv run python -m pytest --cov=core --cov=config --cov=strategies --cov=utils --cov-report=term

# Run a single test file
uv run python -m pytest tests/order_handling/test_order_manager.py

# Run a single test
uv run python -m pytest tests/order_handling/test_order_manager.py::TestClassName::test_method_name

# Integration tests (backtest E2E, no network needed)
uv run python -m pytest tests/integration/test_backtest_e2e.py -v

# Sandbox smoke tests (requires network, validates ccxt/aiohttp compatibility)
uv run python -m pytest -m sandbox -v

# Update snapshots after intentional behavior changes
uv run python -m pytest tests/integration/test_backtest_e2e.py --update-snapshots

# Lint and format (via pre-commit or directly)
uv run ruff check --fix .
uv run ruff format .

# Run pre-commit hooks
uv run pre-commit run --all-files

# Start monitoring stack (Grafana/Loki/Promtail)
docker-compose up -d
```

## Architecture

### Entry Point

`main.py` parses CLI args, creates a `GridTradingBot` per config file, and runs them concurrently via `asyncio.gather`. In live/paper mode, `BotController` (CLI commands) and `HealthCheck` (system monitoring) run as concurrent tasks.

### Core Modules

- **`core/bot_management/`** — Bot lifecycle: `GridTradingBot` (orchestrator), `EventBus` (pub/sub), `BotController` (CLI), `HealthCheck`, `NotificationHandler` (Apprise alerts)
- **`core/grid_management/`** — `GridManager` computes grid levels (arithmetic/geometric spacing), `GridLevel` is a state machine tracking each level's cycle (`READY_TO_BUY` → `WAITING_FOR_BUY_FILL` → `READY_TO_SELL` → etc.)
- **`core/order_handling/`** — `OrderManager` orchestrates order placement and pairing (buy fill → place sell above, sell fill → place buy below). `BalanceTracker` tracks fiat/crypto with reserved amounts. `OrderBook` maps orders to grid levels
- **`core/services/`** — Exchange abstraction via `ExchangeInterface` ABC. `BacktestExchangeService` reads CSV/CCXT OHLCV data. `LiveExchangeService` uses CCXT Pro WebSockets
- **`core/order_handling/execution_strategy/`** — `OrderExecutionStrategyInterface` ABC. Backtest variant is instant; live variant has retry logic with exponential backoff and slippage handling
- **`core/validation/`** — `OrderValidator` for order quantity/price validation

### Strategy Layer

- **`strategies/grid_trading_strategy.py`** — Implements `TradingStrategyInterface`. Handles both backtest (iterate OHLCV rows) and live (WebSocket price stream) execution loops. Manages initial purchase (50% allocation), grid order initialization, take-profit/stop-loss
- **`strategies/trading_performance_analyzer.py`** — Computes ROI, max drawdown, Sharpe ratio, etc.
- **`strategies/plotter.py`** — Plotly visualization for backtest results

### Config & Utils

- **`config/`** — `ConfigManager` (accessor methods for nested JSON), `ConfigValidator` (schema validation), `TradingMode` enum, custom `ConfigError` exceptions
- **`utils/`** — CLI arg parsing, logging setup, config name generation, performance results I/O

### Key Design Patterns

- **Factory Pattern**: `ExchangeServiceFactory` and `OrderExecutionStrategyFactory` select implementations based on `TradingMode`
- **Event Bus**: Decoupled communication via events (`ORDER_FILLED`, `ORDER_CANCELLED`, `START_BOT`, `STOP_BOT`). Components subscribe to events rather than calling each other directly
- **State Machine**: `GridLevel` transitions through `GridCycleState` enum states as orders are placed and filled
- **Strategy Pattern**: `TradingStrategyInterface` ABC; currently one implementation (`GridTradingStrategy`) with two modes (`simple_grid`, `hedged_grid`)

### Trading Modes

Configured via `exchange.trading_mode` in `config.json`:
- **`backtest`**: Simulates on historical OHLCV data (CSV or fetched via CCXT). No real orders
- **`paper_trading`**: Connects to exchange sandbox APIs with real market data. Runs BotController + HealthCheck
- **`live`**: Real trading with actual funds. Same architecture as paper trading but against production APIs

## Testing

Tests mirror the source structure under `tests/`. Async tests use `pytest-asyncio` with `asyncio_mode = "auto"`. The `conftest.py` provides a `valid_config` fixture with a complete config dict. Coverage excludes interface files (`*/interface*.py`).

**Important:** Use `uv run python -m pytest` (not `uv run pytest`) to ensure the correct virtualenv Python is used.

### Unit Tests

Located in `tests/` (excluding `tests/integration/`). Heavy use of mocking to isolate components.

### Integration Tests

Located in `tests/integration/`. **Zero mocks** — the full bot stack runs against real CSV data.

- **`test_backtest_e2e.py`** — 16 parametrized tests covering 4 scenarios (simple_grid/hedged_grid x arithmetic/geometric). Validates: no crash, balance coherence, grid cycle integrity, and deterministic results via snapshot comparison.
- **`test_sandbox_smoke.py`** — 3 tests that make real network calls to Binance public API (REST + WebSocket). Validates ccxt/aiohttp compatibility. Marked `@pytest.mark.sandbox` and excluded from default runs.
- **`snapshots/`** — JSON reference files for deterministic result comparison. Update with `--update-snapshots` flag after intentional behavior changes.

### Pytest Markers

- `integration` — E2E backtest tests
- `sandbox` — Tests requiring network access (excluded by default via `addopts`)

PYTHONPATH must include the project root (CI sets this explicitly).

## Linting & Formatting

Ruff is configured in `pyproject.toml` with 120-char line length, targeting Python 3.12. Pre-commit hooks run Ruff lint+format, plus checks for trailing whitespace, valid YAML/JSON/TOML, merge conflicts, debug statements, blanket `noqa`/`type: ignore`, and deprecated `log.warn()`.
