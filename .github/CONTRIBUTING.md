# Contributing

Thanks for your interest in contributing to **Grid Trading Bot**!

## Getting Started

1. Fork the repository and clone your fork.
2. Install dependencies:
   ```bash
   uv sync --all-extras --dev
   ```
3. Create a branch for your changes:
   ```bash
   git checkout -b my-feature
   ```

## Development Workflow

### Running the bot

```bash
uv run grid_trading_bot run --config config/config.json
```

### Linting & Formatting

The project uses [Ruff](https://docs.astral.sh/ruff/) (120-char line length, Python 3.12). Pre-commit hooks are configured — install them with:

```bash
uv run pre-commit install
```

You can also run them manually:

```bash
uv run pre-commit run --all-files
```

### Running Tests

```bash
# All tests with coverage
uv run python -m pytest --cov=grid_trading_bot --cov-report=term

# Integration tests only
uv run python -m pytest tests/integration/test_backtest_e2e.py -v
```

## Pull Requests

- Keep PRs focused on a single change.
- Add or update tests for any new behavior.
- Make sure all tests pass and pre-commit hooks are green before opening a PR.
- Fill out the [PR template](.github/PULL_REQUEST_TEMPLATE.md) when submitting.

## Reporting Issues

Open an issue describing the problem, including steps to reproduce, expected behavior, and your environment (OS, Python version, etc.).
