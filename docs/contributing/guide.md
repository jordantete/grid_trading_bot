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

Requires network access. Validates CCXT (and the aiohttp transport it brings) against live exchange APIs:

```bash
uv run python -m pytest -m sandbox -v
```

### Persistence layer tests

Tests for SQLite state persistence, serialization, and recovery logic:

```bash
uv run python -m pytest tests/persistence/ -v
```

### Update snapshots

The backtest snapshots in `tests/integration/snapshots/` hold every performance metric at full precision, the final balances and the list of fills. A missing snapshot fails its test with a message naming the option below.

After an intentional behavior change, rewrite them:

```bash
uv run python -m pytest tests/integration/test_backtest_e2e.py --update-snapshots
```

The tests pass, and the end of the run lists every field and fill that changed against the previous files. Read that list before committing the new snapshots.

## Linting, Formatting and Type Checking

The project uses [Ruff](https://github.com/astral-sh/ruff) for linting and formatting, and [mypy](https://mypy-lang.org/) for type checking:

```bash
# Lint with auto-fix
uv run ruff check --fix .

# Format
uv run ruff format .

# Type check (src/ only, must report 0 errors)
uv run mypy

# Run all pre-commit hooks
uv run pre-commit run --all-files
```

mypy is configured in `pyproject.toml`. A `# type: ignore` needs its error code and a one-line reason, for example `# type: ignore[arg-type]  # ccxt declares str, the API takes int`.

The pre-commit hooks for Ruff and mypy run from the project environment (`uv run --frozen --no-sync`), so they need an existing `.venv`: run `uv sync --all-extras --dev` once before the first commit.

## Code Style

- **Line length**: 120 characters
- **Python version**: 3.12+
- **Quotes**: Double quotes
- **Imports**: Sorted by isort (via Ruff), full package paths (`from grid_trading_bot.config.X import Y`)

Pre-commit hooks enforce:

- Ruff lint + format and mypy, with the versions pinned in `pyproject.toml`
- Trailing whitespace removal
- Valid YAML/JSON/TOML
- No merge conflict markers
- No debug statements
- No blanket `noqa` / `type: ignore`
- No deprecated `log.warn()`

## Project Structure

- Source code: `src/grid_trading_bot/`
- Tests: `tests/` (mirrors source structure)
- Persistence tests: `tests/persistence/`
- Integration tests: `tests/integration/`
- Config files: `config/`
- Monitoring stack: `monitoring/`

## Continuous Integration

`.github/workflows/ci.yml` runs on every push to `master` and every pull request:

- `test`: the test suite on Python 3.12 and 3.14, with coverage uploaded to Codecov from 3.12
- `lint`: `ruff check`, `ruff format --check` and `mypy`

## Releasing

The version is written once, in `pyproject.toml`. `grid_trading_bot.__version__` and `grid_trading_bot --version` read it from the installed package.

1. Set `version` in `pyproject.toml`, run `uv lock` (the lock records the project version), commit both on `master` and push.
2. Tag that commit and push the tag:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

3. The `release` workflow checks that the tag is `v` plus the `pyproject.toml` version and that the commit is on `master` (`scripts/verify_release_tag.sh`), runs the CI, builds the package and checks it with `twine check`.
4. Publishing waits for approval of the `pypi` environment: open the run in the Actions tab and approve the deployment. The package goes to PyPI, then the GitHub release is created with generated notes.

If the tag check fails, delete the tag (`git push --delete origin vX.Y.Z` and `git tag -d vX.Y.Z`), fix the cause, and tag again.

## Pull Request Process

1. Fork the repository and create a feature branch from `master`
2. Make your changes following the code style above
3. Add or update tests as appropriate
4. Ensure all tests pass and pre-commit hooks succeed
5. Submit a pull request with a clear description of the changes

## Reporting Issues

If you encounter bugs or have feature requests, please [open an issue](https://github.com/jordantete/grid_trading_bot/issues) on GitHub.
