# Contributing Guide

Contributions are welcome! Whether it's bug fixes, new features, documentation improvements, or test coverage.

## Development Setup

```bash
git clone https://github.com/jordantete/grid_trading_bot.git
cd grid_trading_bot
uv sync --all-extras --dev
```

## Running Tests

### All tests (unit + integration)

```bash
uv run python -m pytest --cov=grid_trading_bot --cov-report=term
```

### Single test file

```bash
uv run python -m pytest tests/order_handling/test_order_manager.py
```

### Single test

```bash
uv run python -m pytest tests/order_handling/test_order_manager.py::TestClassName::test_method_name
```

### Integration tests (E2E backtest)

Zero mocks — runs the full bot stack against real CSV data:

```bash
uv run python -m pytest tests/integration/test_backtest_e2e.py -v
```

### Sandbox smoke tests

Requires network access — validates CCXT/aiohttp compatibility with live exchange APIs:

```bash
uv run python -m pytest -m sandbox -v
```

### Update snapshots

After intentional behavior changes, update the reference snapshots:

```bash
uv run python -m pytest tests/integration/test_backtest_e2e.py --update-snapshots
```

## Linting and Formatting

The project uses [Ruff](https://github.com/astral-sh/ruff) for both linting and formatting:

```bash
# Lint with auto-fix
uv run ruff check --fix .

# Format
uv run ruff format .

# Run all pre-commit hooks
uv run pre-commit run --all-files
```

## Code Style

- **Line length**: 120 characters
- **Python version**: 3.12+
- **Quotes**: Double quotes
- **Imports**: Sorted by isort (via Ruff), full package paths (`from grid_trading_bot.config.X import Y`)

Pre-commit hooks enforce:

- Ruff lint + format
- Trailing whitespace removal
- Valid YAML/JSON/TOML
- No merge conflict markers
- No debug statements
- No blanket `noqa` / `type: ignore`
- No deprecated `log.warn()`

## Project Structure

- Source code: `src/grid_trading_bot/`
- Tests: `tests/` (mirrors source structure)
- Integration tests: `tests/integration/`
- Config files: `config/`
- Monitoring stack: `monitoring/`

## Pull Request Process

1. Fork the repository and create a feature branch from `master`
2. Make your changes following the code style above
3. Add or update tests as appropriate
4. Ensure all tests pass and pre-commit hooks succeed
5. Submit a pull request with a clear description of the changes

## Reporting Issues

If you encounter bugs or have feature requests, please [open an issue](https://github.com/jordantete/grid_trading_bot/issues) on GitHub.
