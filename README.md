# Grid Trading Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/jordantete/grid_trading_bot/actions/workflows/unit_tests.yml/badge.svg)](https://github.com/jordantete/grid_trading_bot/actions/workflows/unit_tests.yml)
[![codecov](https://codecov.io/github/jordantete/grid_trading_bot/graph/badge.svg?token=DOZRQAXAK7)](https://codecov.io/github/jordantete/grid_trading_bot)
[![PyPI version](https://img.shields.io/pypi/v/grid-trading-bot)](https://pypi.org/project/grid-trading-bot/)
[![Documentation](https://img.shields.io/badge/docs-GitHub%20Pages-blue)](https://jordantete.github.io/grid_trading_bot/)

**Backtest. Paper trade. Go live.**
A modular Python engine for crypto grid trading with real-time Grafana monitoring.

---

## Features

|                           |                                                                             |
| ------------------------- | --------------------------------------------------------------------------- |
| **Backtesting**           | Simulate grid strategies on historical OHLCV data (CSV or fetched via CCXT) |
| **Paper Trading**         | Test against real market data using exchange sandbox APIs                   |
| **Live Trading**          | Execute real trades with retry logic and circuit breakers                   |
| **Grid Strategies**       | Simple grid and hedged grid with arithmetic or geometric spacing            |
| **Risk Management**       | Configurable take-profit, stop-loss, and slippage controls                  |
| **Performance Analytics** | ROI, max drawdown, Sharpe ratio, interactive Plotly charts                  |
| **Monitoring**            | Grafana dashboards with Loki log aggregation                                |
| **Multi-Exchange**        | Any exchange supported by CCXT                                              |

## Quick Start

```bash
git clone https://github.com/jordantete/grid_trading_bot.git
cd grid_trading_bot
uv sync --all-extras --dev
uv run grid_trading_bot run --config config/config.json
```

## Documentation

> **[Full documentation](https://jordantete.github.io/grid_trading_bot/)** — Installation, configuration reference, CLI usage, monitoring setup, architecture guide, and more.

| Resource                                                                                      | Description                             |
| --------------------------------------------------------------------------------------------- | --------------------------------------- |
| [Quick Start](https://jordantete.github.io/grid_trading_bot/quick-start/)                     | Install and run your first backtest     |
| [Configuration](https://jordantete.github.io/grid_trading_bot/configuration/config-file/)     | Full `config.json` parameter reference  |
| [Grid Trading Concepts](https://jordantete.github.io/grid_trading_bot/concepts/grid-trading/) | Understand grid trading strategies      |
| [Monitoring Setup](https://jordantete.github.io/grid_trading_bot/monitoring/setup/)           | Set up Grafana dashboards for live bots |
| [Architecture](https://jordantete.github.io/grid_trading_bot/architecture/overview/)          | Codebase design and patterns            |
| [Contributing](https://jordantete.github.io/grid_trading_bot/contributing/guide/)             | Help improve the project                |

## License

This project is licensed under the MIT License. See the [LICENSE](./LICENSE.txt) file for details.

## Disclaimer

This project is intended for educational purposes only. The authors and contributors are not responsible for any financial losses incurred while using this bot. Trading cryptocurrencies involves significant risk and can result in the loss of all invested capital. Please do your own research and consult with a licensed financial advisor before making any trading decisions. Use this software at your own risk.
