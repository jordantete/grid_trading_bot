# CLI Reference

The bot provides a command-line interface built with [Click](https://click.palletsprojects.com/).

## Installation

Once installed, the `grid_trading_bot` command is available:

```bash
# Via uv (recommended)
uv run grid_trading_bot <command>

# Or directly if installed in your environment
grid_trading_bot <command>

# Or via python -m
uv run python -m grid_trading_bot <command>
```

## Commands

### `run`

Run the trading bot with one or more configuration files.

```bash
uv run grid_trading_bot run --config config/config.json
```

**Options:**

| Option | Type | Required | Description |
|--------|------|----------|-------------|
| `--config` | string | Yes | Path to configuration file. Can be specified multiple times for concurrent execution. |
| `--save_performance_results` | string | No | Path to save simulation results as JSON (e.g., `results.json`). |
| `--no-plot` | flag | No | Disable interactive Plotly charts at the end of simulation. |
| `--profile` | flag | No | Enable profiling for performance analysis. |

### `--version`

Display the installed version:

```bash
uv run grid_trading_bot --version
```

## Usage Examples

### Basic backtest

```bash
uv run grid_trading_bot run --config config/config.json
```

### Multiple configurations

Run several strategies concurrently. Each config runs as an independent bot instance via `asyncio.gather`:

```bash
uv run grid_trading_bot run \
  --config config/config1.json \
  --config config/config2.json \
  --config config/config3.json
```

### Save performance results

Export metrics to a JSON file for later analysis:

```bash
uv run grid_trading_bot run --config config/config.json --save_performance_results results.json
```

### Headless mode (no plots)

Useful for CI pipelines or batch runs:

```bash
uv run grid_trading_bot run --config config/config.json --no-plot
```

### Combined options

```bash
uv run grid_trading_bot run \
  --config config/config1.json \
  --config config/config2.json \
  --save_performance_results combined_results.json \
  --no-plot
```

### With profiling

Generate a `.prof` file for performance analysis:

```bash
uv run grid_trading_bot run --config config/config.json --profile
```
