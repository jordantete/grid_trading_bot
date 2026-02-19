# Grid Trading Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![on_push_or_merge_pr_master](https://github.com/jordantete/grid_trading_bot/actions/workflows/run-tests-on-push-or-merge-pr-master.yml/badge.svg)](https://github.com/jordantete/grid_trading_bot/actions/workflows/run-tests-on-push-or-merge-pr-master.yml)
[![codecov](https://codecov.io/github/jordantete/grid_trading_bot/graph/badge.svg?token=DOZRQAXAK7)](https://codecov.io/github/jordantete/grid_trading_bot)
[![PyPI version](https://img.shields.io/pypi/v/grid-trading-bot)](https://pypi.org/project/grid-trading-bot/)

Open-source Grid Trading Bot implemented in Python, allowing you to backtest and execute grid trading strategies on cryptocurrency markets. The bot is highly customizable and works with various exchanges using the CCXT library.

## 📚 Table of Contents

- [Grid Trading Bot](#grid-trading-bot)
- [Features](#features)
- [🤔 What is Grid Trading?](#-what-is-grid-trading)
  - [🔢 Arithmetic Grid Trading](#-arithmetic-grid-trading)
  - [📐 Geometric Grid Trading](#-geometric-grid-trading)
  - [📅 When to Use Each Type?](#-when-to-use-each-type)
  - [🆚 Simple Grid vs. Hedged Grid Strategies](#-simple-grid-vs-hedged-grid-strategies)
- [🖥️ Installation](#️-installation)
  - [Prerequisites](#prerequisites)
  - [Setting Up the Environment](#setting-up-the-environment)
- [📋 Configuration](#-configuration)
  - [Example Configuration File](#example-configuration-file)
  - [Parameters](#parameters)
  - [Environment Variables (.env)](#environment-variables-env)
- [🏃 Running the Bot](#-running-the-bot)
  - [Basic Usage](#basic-usage)
  - [Multiple Configurations](#multiple-configurations)
  - [Saving Performance Results](#saving-performance-results)
  - [Disabling Plots](#disabling-plots)
  - [Combining Options](#combining-options)
  - [Available Command-Line Arguments](#available-command-line-arguments)
- [📊 Docker Compose for Logs Management](#-docker-compose-for-logs-management)
  - [Steps to Set Up](#steps-to-set-up)
- [🤝 Contributing](#-contributing)
  - [Reporting Issues](#reporting-issues)
- [💸 Donations](#-donations)
- [📜 License](#-license)
- [🚨 Disclaimer](#-disclaimer)

## Features

- **Backtesting**: Simulate your grid trading strategy using historical data.
- **Live Trading**: Execute trades on live markets using real funds, supported by robust configurations and risk management.
- **Paper Trading**: Test strategies in a simulated live market environment without risking actual funds.
- **Multiple Grid Trading Strategies**: Implement different grid trading strategies to match market conditions.
- **Customizable Configurations**: Use a JSON file to define grid levels, strategies, and risk settings.
- **Support for Multiple Exchanges**: Seamless integration with multiple cryptocurrency exchanges via the CCXT library.
- **Take Profit & Stop Loss**: Safeguard your investments with configurable take profit and stop loss thresholds.
- **Performance Metrics**: Gain insights with comprehensive metrics like ROI, max drawdown, run-up, and more.
- **HealthCheck**: Continuously monitor the bot’s performance and system resource usage to ensure stability.
- **CLI BotController**: Control and interact with the bot in real time using intuitive commands.
- **Logging with Grafana**: Centralized logging system for monitoring bot activity and debugging, enhanced with visual dashboards.

## 🤔 What is Grid Trading?

Grid trading is a trading strategy that places buy and sell orders at predefined intervals above and below a set price. The goal is to capitalize on market volatility by buying low and selling high at different price points. There are two primary types of grid trading: **arithmetic** and **geometric**.

### 🔢 **Arithmetic Grid Trading**

In an arithmetic grid, the grid levels (price intervals) are spaced **equally**. The distance between each buy and sell order is constant, providing a more straightforward strategy for fluctuating markets.

#### **Example**

Suppose the price of a cryptocurrency is $3000, and you set up a grid with the following parameters:

- **Grid levels**: $2900, $2950, $3000, $3050, $3100
- **Buy orders**: Set at $2900 and $2950
- **Sell orders**: Set at $3050 and $3100

As the price fluctuates, the bot will automatically execute buy orders as the price decreases and sell orders as the price increases. This method profits from small, predictable price fluctuations, as the intervals between buy/sell orders are consistent (in this case, $50).

### 📐 **Geometric Grid Trading**

In a geometric grid, the grid levels are spaced **proportionally** or by a percentage. The intervals between price levels increase or decrease exponentially based on a set percentage, making this grid type more suited for assets with higher volatility.

#### **Simple Example**

Suppose the price of a cryptocurrency is $3000, and you set up a geometric grid with a 5% spacing between levels. The price intervals will not be equally spaced but will grow or shrink based on the percentage.

- **Grid levels**: $2700, $2835, $2975, $3125, $3280
- **Buy orders**: Set at $2700 and $2835
- **Sell orders**: Set at $3125 and $3280

As the price fluctuates, buy orders are executed at lower levels and sell orders at higher levels, but the grid is proportional. This strategy is better for markets that experience exponential price movements.

### 📅 **When to Use Each Type?**

- **Arithmetic grids** are ideal for assets with more stable, linear price fluctuations.
- **Geometric grids** are better for assets with significant, unpredictable volatility, as they adapt more flexibly to market swings.


### 🆚 Simple Grid vs. Hedged Grid Strategies

- **Simple Grid**: Independent buy and sell grids. Profits from each grid level are standalone.
- **Hedged Grid**: Pairs buy and sell levels dynamically, balancing risk and reward for higher volatility markets.

## 🖥️ Installation

### Prerequisites

This project leverages [uv](https://github.com/astral-sh/uv) for managing virtual environments and dependencies. Below, you’ll find instructions for getting started with uv, along with an alternative approach using **venv**. While not covered in detail here, you can also easily set up the project using **Poetry**.

### Setting Up the Environment

#### Using `uv` (Recommended)

1. **Install `uv` (if not already installed)**
   Ensure `uv` is installed on your system. If not, install it with `pip`:
  ```sh
  pip install uv
  ```

2. **Clone the repository**:
  ```sh
  git clone https://github.com/jordantete/grid_trading_bot.git
  cd grid_trading_bot
  ```

3.  **Install Dependencies and Set Up Virtual Environment**:
  Run the following command to automatically set up a virtual environment and install all dependencies defined in `pyproject.toml`:
  ```sh
   uv sync --all-extras --dev
  ```

#### Using `venv` and `pip` (Alternative)

1. **Clone the repository**:
  ```sh
  git clone https://github.com/jordantete/grid_trading_bot.git
  cd grid_trading_bot
  ```

2. **Set up a virtual environment**:
  Create and activate a virtual environment:

  ```sh
  python3 -m venv .venv
  source .venv/bin/activate  # On Windows: .venv\Scripts\activate
  ```

2. **Install dependencies**:
  Use pip to install the dependencies listed in `pyproject.toml`:

  ```sh
  pip install -r requirements.txt
  ```

  Note: You may need to generate a requirements.txt file from pyproject.toml if it’s not already present. You can use a tool like pipreqs or manually extract dependencies.

## 📋 Configuration

The bot is configured via a JSON file `config/config.json` to suit your trading needs, alongside a `.env` file to securely store sensitive credentials and environment variables. Below is an example configuration file and a breakdown of all parameters.

### **Example Configuration File**
```json
{
  "exchange": {
    "name": "binance",
    "trading_fee": 0.001,
    "trading_mode": "backtest"
  },
  "pair": {
    "base_currency": "SOL",
    "quote_currency": "USDT"
  },
  "trading_settings": {
    "timeframe": "1m",
    "period": {
      "start_date": "2024-08-01T00:00:00Z",
      "end_date": "2024-10-20T00:00:00Z"
    },
    "initial_balance": 10000,
    "historical_data_file": "data/SOL_USDT/2024/1m.csv"
  },
  "grid_strategy": {
    "type": "simple_grid",
    "spacing": "geometric",
    "num_grids": 8,
    "range": {
      "top": 200,
      "bottom": 250
    }
  },
  "risk_management": {
    "take_profit": {
      "enabled": false,
      "threshold": 300
    },
    "stop_loss": {
      "enabled": false,
      "threshold": 150
    }
  },
  "execution": {
    "max_retries": 3,
    "retry_delay": 1.0,
    "max_slippage": 0.01,
    "backtest_slippage": 0.001,
    "order_polling_interval": 15.0,
    "websocket_max_retries": 5,
    "websocket_retry_base_delay": 5,
    "health_check_interval": 60,
    "circuit_breaker_failure_threshold": 5,
    "circuit_breaker_recovery_timeout": 60.0,
    "circuit_breaker_half_open_max_calls": 1
  },
  "logging": {
    "log_level": "INFO",
    "log_to_file": true
  }
}
```

### **Parameters**

- **exchange**: Defines the exchange and trading fee to be used.
  - **name**: The name of the exchange (e.g., binance).
  - **trading_fee**: The trading fee should be in decimal format (e.g., 0.001 for 0.1%).
  - **trading_mode**: The trading mode of operation (backtest, live or paper trading).

- **pair**: Specifies the trading pair.
  - **base_currency**: The base currency (e.g., ETH).
  - **quote_currency**: The quote currency (e.g., USDT).

- **trading_settings**: General trading settings.
  - **timeframe**: Time interval for the data (e.g., `1m` for one minute).
  - **period**: The start and end dates for the backtest or trading period.
    - **start_date**: The start date of the trading or backtest period.
    - **end_date**: The end date of the trading or backtest period.
  - **initial_balance**: Starting balance for the bot.
  - **historical_data_file**: Path to a local historical data file for offline testing (optional).

- **grid_strategy**: Defines the grid trading parameters.
  - **type**: Type of grid strategy:
    - **simple_grid**: Independent buy/sell levels.
    - **hedged_grid**: Dynamically paired buy/sell levels for risk balancing.
  - **spacing**: Grid spacing type:
    - **arithmetic**: Equal price intervals.
    - **geometric**: Proportional price intervals based on percentage.
  - **num_grids**: The total number of grid levels.
  - **range**: Defines the price range of the grid.
    - **top**: The upper price limit of the grid.
    - **bottom**: The lower price limit of the grid.

- **risk_management**: Configurations for risk management.
  - **take_profit**: Settings for taking profit.
    - **enabled**: Whether the take profit is active.
    - **threshold**: The price at which to take profit.
  - **stop_loss**: Settings for stopping loss.
    - **enabled**: Whether the stop loss is active.
    - **threshold**: The price at which to stop loss.

- **execution** *(optional)*: Fine-tunes order execution behavior. All fields are optional and have sensible defaults.
  - **max_retries**: Maximum retry attempts for failed order placement in live/paper mode. Integer, `1`–`20`. Default: `3`.
  - **retry_delay**: Delay in seconds between retries. Float, `0.1`–`60.0`. Default: `1.0`.
  - **max_slippage**: Maximum acceptable slippage for live/paper order execution (e.g., `0.01` = 1%). Float, `0.0001`–`0.1`. Default: `0.01`.
  - **backtest_slippage**: Fixed slippage percentage applied to every simulated fill during backtesting (e.g., `0.001` = 0.1%). Buys fill at a slightly higher price, sells at a slightly lower price. Useful for stress-testing strategy profitability. Float, `0.0`–`0.1`. Default: `0.0` (no slippage).
  - **order_polling_interval**: Interval in seconds for polling open order status in live/paper mode. Float, `1.0`–`300.0`. Default: `15.0`.
  - **websocket_max_retries**: Maximum reconnection attempts for the WebSocket feed. Integer, `1`–`50`. Default: `5`.
  - **websocket_retry_base_delay**: Base delay in seconds for WebSocket reconnection backoff. Integer, `1`–`120`. Default: `5`.
  - **health_check_interval**: Interval in seconds between health check pings. Integer, `10`–`3600`. Default: `60`.
  - **circuit_breaker_failure_threshold**: Number of consecutive exchange API failures before the circuit breaker opens. Integer, `1`–`50`. Default: `5`.
  - **circuit_breaker_recovery_timeout**: Seconds to wait before attempting recovery after circuit breaker opens. Float, `1.0`–`600.0`. Default: `60.0`.
  - **circuit_breaker_half_open_max_calls**: Maximum test calls allowed in half-open state. Integer, `1`–`10`. Default: `1`.

- **logging**: Configures logging settings.
  - **log_level**: The logging level (e.g., `INFO`, `DEBUG`).
  - **log_to_file**: Enables logging to a file.

### **Environment Variables (.env)**

The `.env` file securely stores sensitive data like API keys and credentials. Below is an example:

```
# Exchange API credentials
EXCHANGE_API_KEY=YourExchangeAPIKeyHere
EXCHANGE_SECRET_KEY=YourExchangeSecretKeyHere

# Notification URLs for Apprise
APPRISE_NOTIFICATION_URLS=

# Grafana Admin Access
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=YourGrafanaPasswordHere
```

**Environment Variables Breakdown**

- `EXCHANGE_API_KEY`: Your API key for the exchange.
- `EXCHANGE_SECRET_KEY`: Your secret key for the exchange.
- `APPRISE_NOTIFICATION_URLS`: URLs for notifications (e.g., Telegram bot, Discord Server). For detailed setup instructions, visit the [Apprise GitHub repository](https://github.com/caronc/apprise).
- `GRAFANA_ADMIN_USER`: Admin username for Grafana.
- `GRAFANA_ADMIN_PASSWORD`: Admin password for Grafana.

## 🏃 Running the Bot

To run the bot, use the following command:

> **Note:** If you're using `uv` to manage your virtual environment, make sure to prefix the command with `uv run` to ensure it runs within the environment.

### Basic Usage:
  ```sh
  uv run grid_trading_bot run --config config/config.json
  ```

### Multiple Configurations:
If you want to run the bot with multiple configuration files simultaneously, you can specify them all:
  ```sh
  uv run grid_trading_bot run --config config/config1.json --config config/config2.json --config config/config3.json
  ```

### Saving Performance Results:
To save the performance results to a file, use the **--save_performance_results** option:
  ```sh
  uv run grid_trading_bot run --config config/config.json --save_performance_results results.json
  ```

### Disabling Plots:
To run the bot without displaying the end-of-simulation plots, use the **--no-plot** flag:
  ```sh
  uv run grid_trading_bot run --config config/config.json --no-plot
  ```

### Combining Options:
You can combine multiple options to customize how the bot runs. For example:
  ```sh
  uv run grid_trading_bot run --config config/config1.json --config config/config2.json --save_performance_results combined_results.json --no-plot
  ```

### Available Command-Line Arguments:

| **Argument**                  | **Type**   | **Required** | **Description**                                                                 |
|-------------------------------|------------|--------------|---------------------------------------------------------------------------------|
| `--config`                    | `str`      | ✅ Yes       | Path(s) to configuration file(s). Multiple files can be provided.              |
| `--save_performance_results`  | `str`      | ❌ No        | Path to save simulation results (e.g., `results.json`).                        |
| `--no-plot`                   | `flag`     | ❌ No        | Disable the display of plots at the end of the simulation.                     |
| `--profile`                   | `flag`     | ❌ No        | Enable profiling to analyze performance metrics during execution.              |


## 📊 Docker Compose for Logs Management

A `docker-compose.yml` file is included to set up centralized logging using Grafana, Loki, and Promtail. This allows you to monitor and analyze the bot's logs efficiently.

### Steps to Set Up:

1. **Ensure Docker and Docker Compose Are Installed**
  Verify that Docker and Docker Compose are installed on your system. If not, follow the official [Docker installation guide](https://docs.docker.com/get-docker/).


2. **Start the Services**
  Run the following command to spin up Grafana, Loki, and Promtail:

  ```sh
  docker-compose up -d
  ```


3. **Access Grafana Dashboards**

  Navigate to http://localhost:3000 in your browser to access the Grafana dashboard.
  Use the following default credentials to log in:

	- Username: admin
	- Password: YourGrafanaPasswordHere (as defined in the .env file)

4. **Import Dashboards**

  Go to the Dashboards section in Grafana and click Import. Use the provided JSON file for predefined dashboards. This file can be found in the project directory: ```grafana/dashboards/grid_trading_bot_dashboard.json```


## 🤝 Contributing

Contributions are welcome! If you have suggestions or want to improve the bot, feel free to fork the repository and submit a pull request.

### Reporting Issues

If you encounter any issues or have feature requests, please create a new issue on the [GitHub Issues](https://github.com/pownedjojo/grid_trading_bot/issues) page.

## 💸 Donations

If you find this project helpful and would like to support its development, consider buying me a coffee! Your support is greatly appreciated and motivates me to continue improving and adding new features.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/pownedj)

Thank you for your support!

## 📜 License

This project is licensed under the MIT License. See the [LICENSE](./LICENSE.txt) file for more details.

## 🚨 Disclaimer

This project is intended for educational purposes only. The authors and contributors are not responsible for any financial losses incurred while using this bot. Trading cryptocurrencies involves significant risk and can result in the loss of all invested capital. Please do your own research and consult with a licensed financial advisor before making any trading decisions. Use this software at your own risk.
