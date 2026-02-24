# Grid Trading Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/jordantete/grid_trading_bot/actions/workflows/run-tests-on-push-or-merge-pr-master.yml/badge.svg)](https://github.com/jordantete/grid_trading_bot/actions/workflows/run-tests-on-push-or-merge-pr-master.yml)
[![codecov](https://codecov.io/github/jordantete/grid_trading_bot/graph/badge.svg?token=DOZRQAXAK7)](https://codecov.io/github/jordantete/grid_trading_bot)
[![PyPI version](https://img.shields.io/pypi/v/grid-trading-bot)](https://pypi.org/project/grid-trading-bot/)

An open-source cryptocurrency grid trading bot implemented in Python. Backtest strategies on historical data, paper trade with sandbox APIs, or go live on real markets — all from a single, highly customizable configuration.

## Key Features

- **Backtesting** — Simulate grid strategies on historical OHLCV data (CSV or fetched via CCXT)
- **Paper Trading** — Test against real market data using exchange sandbox APIs
- **Live Trading** — Execute real trades with robust retry logic and circuit breakers
- **Multiple Strategies** — Simple grid and hedged grid with arithmetic or geometric spacing
- **Risk Management** — Configurable take-profit, stop-loss, and slippage controls
- **Performance Metrics** — ROI, max drawdown, Sharpe ratio, and interactive Plotly charts
- **Real-time Monitoring** — Grafana dashboards with Loki log aggregation
- **Multi-Exchange Support** — Any exchange supported by CCXT

## Quick Links

<div class="grid cards" markdown>

- :material-rocket-launch: **[Quick Start](quick-start.md)** — Install and run your first backtest in under 2 minutes
- :material-cog: **[Configuration](configuration/config-file.md)** — Full reference for `config.json` parameters
- :material-chart-line: **[Concepts](concepts/grid-trading.md)** — Understand grid trading strategies
- :material-monitor-dashboard: **[Monitoring](monitoring/setup.md)** — Set up Grafana dashboards for live bots
- :material-file-tree: **[Architecture](architecture/overview.md)** — Explore the codebase design and patterns
- :material-account-group: **[Contributing](contributing/guide.md)** — Help improve the project

</div>
