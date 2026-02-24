# Quick Start

Get up and running with your first backtest in under 2 minutes.

## Prerequisites

- **Python 3.12+**
- **[uv](https://github.com/astral-sh/uv)** (recommended) or pip

## Installation

### Using uv (Recommended)

```bash
git clone https://github.com/jordantete/grid_trading_bot.git
cd grid_trading_bot
uv sync --all-extras --dev
```

### Using venv and pip

```bash
git clone https://github.com/jordantete/grid_trading_bot.git
cd grid_trading_bot
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
```

## Run Your First Backtest

The repository ships with a default configuration file at `config/config.json` set to backtest mode:

```bash
uv run grid_trading_bot run --config config/config.json
```

This will:

1. Load historical OHLCV data for the configured trading pair
2. Execute the grid trading strategy over the specified period
3. Display an interactive Plotly chart with price action and order execution
4. Print performance metrics (ROI, max drawdown, Sharpe ratio, etc.)

!!! tip "Disable plots"
    Add `--no-plot` to skip the chart display, useful for CI or batch runs:
    ```bash
    uv run grid_trading_bot run --config config/config.json --no-plot
    ```

## Next Steps

- [Configure your own strategy](configuration/config-file.md) — Adjust grid levels, spacing, and risk parameters
- [Understand grid trading concepts](concepts/grid-trading.md) — Learn about arithmetic vs. geometric grids
- [CLI reference](usage/cli.md) — Explore all command-line options
- [Set up monitoring](monitoring/setup.md) — Visualize live bot activity with Grafana
