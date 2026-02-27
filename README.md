# Grid Trading Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/jordantete/grid_trading_bot/actions/workflows/unit_tests.yml/badge.svg)](https://github.com/jordantete/grid_trading_bot/actions/workflows/unit_tests.yml)
[![codecov](https://codecov.io/github/jordantete/grid_trading_bot/graph/badge.svg?token=DOZRQAXAK7)](https://codecov.io/github/jordantete/grid_trading_bot)
[![PyPI version](https://img.shields.io/pypi/v/grid-trading-bot)](https://pypi.org/project/grid-trading-bot/)
[![Documentation](https://img.shields.io/badge/docs-GitHub%20Pages-blue)](https://jordantete.github.io/grid_trading_bot/)

Open-source cryptocurrency grid trading bot in Python. Backtest on historical data, paper trade on sandbox APIs, or go live — with full Grafana monitoring.

## Features

- **Backtesting** on historical OHLCV data (CSV or fetched via CCXT)
- **Paper Trading** and **Live Trading** with retry logic and circuit breakers
- **Simple Grid** and **Hedged Grid** strategies with arithmetic or geometric spacing
- **Risk Management** — configurable take-profit, stop-loss, and slippage
- **Performance Metrics** — ROI, max drawdown, Sharpe ratio, interactive Plotly charts
- **Monitoring** — Grafana dashboards with Loki log aggregation

## Quick Start

```bash
git clone https://github.com/jordantete/grid_trading_bot.git
cd grid_trading_bot
uv sync --all-extras --dev
uv run grid_trading_bot run --config config/config.json
```

> **[Full documentation](https://jordantete.github.io/grid_trading_bot/)** — Installation, configuration reference, CLI usage, monitoring setup, architecture guide, and more.

## Donations

If you find this project helpful, consider buying me a coffee!

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/pownedj)

## License

This project is licensed under the MIT License. See the [LICENSE](./LICENSE.txt) file for details.

## Disclaimer

This project is intended for educational purposes only. The authors and contributors are not responsible for any financial losses incurred while using this bot. Trading cryptocurrencies involves significant risk and can result in the loss of all invested capital. Please do your own research and consult with a licensed financial advisor before making any trading decisions. Use this software at your own risk.
