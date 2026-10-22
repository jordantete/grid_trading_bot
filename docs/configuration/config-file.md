# Configuration File Reference

The bot is configured via a JSON file (typically `config/config.json`). This page documents every available parameter.

## Complete Example

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
    },
    "buy_ratio": 1.0,
    "sell_ratio": 0.5
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
    "circuit_breaker_half_open_max_calls": 1,
    "reconciliation_interval": 300.0,
    "reconciliation_balance_tolerance": 0.01
  },
  "logging": {
    "log_level": "INFO",
    "log_to_file": true
  },
  "persistence": {
    "enabled": true,
    "db_path": "data/SOL_USDT/state.db"
  }
}
```

## Parameter Reference

### `exchange`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Exchange name (e.g., `binance`, `kraken`). Must be supported by CCXT. |
| `trading_fee` | float | Yes | Flat trading fee in decimal format (e.g., `0.001` for 0.1%). Used for both sides unless `maker_fee` / `taker_fee` are set. |
| `maker_fee` | float | No | Fee charged on liquidity-adding fills — resting grid limit orders. Defaults to `trading_fee`. |
| `taker_fee` | float | No | Fee charged on liquidity-taking fills — the initial purchase, take-profit, stop-loss and liquidation market orders. Defaults to `trading_fee`. |
| `post_only` | bool | No | Post grid limit orders maker-only. Defaults to `false`. |
| `trading_mode` | string | Yes | One of `backtest`, `paper_trading`, or `live`. `paper_trading` needs an exchange with a test environment in ccxt (Binance, Bybit, OKX with ccxt 4.5.87); any other exchange is refused at start. |

#### Maker and taker fees

Grid limit orders are meant to rest on the book and pay the maker fee, while the market
orders the bot places on its own (initial purchase, take-profit, stop-loss, liquidation)
cross the spread and pay the taker fee. On venues where the two rates differ — Kraken
(0.16% / 0.26%) or Coinbase (0.60% / 1.20%) at low volume tiers, versus Binance spot which
charges the same on both sides — declaring only `trading_fee` under-costs whichever side is
cheaper in the config and distorts backtest results.

Both keys default to `trading_fee`, so an existing single-rate config keeps behaving exactly
as before.

Fees are charged in the currency received: a buy pays its fee in the base currency, so the bot books the filled quantity minus the fee as crypto received and sizes the sell that follows on that net quantity; a sell pays it in the quote currency. This matches Binance spot without BNB fee payment and OKX, in every trading mode, backtests included. In live and paper trading, a fee the exchange reports on the order replaces the configured rate.

#### Post-only orders

`post_only` sends grid limit orders as maker-only: the exchange **rejects** an order that
would cross the spread instead of filling it as a taker. It is never applied to market
orders, which cross by definition.

A rejection is treated as an expected market condition, not a failure. The bot:

- keeps the placement in its journal, with the grid level and the funds held for it;
- retries it at every order poll (`execution.order_polling_interval`) until the exchange accepts it;
- logs at `INFO` and sends no failure notification;
- does not count the rejection toward the circuit breaker's failure threshold.

The order is never re-priced away from its grid level: that would place it off-grid and
silently distort the profit spacing the whole strategy is built on.

!!! tip "Recommended for live trading"

    `config.live.example.json` ships with `post_only: true`. The default is `false` so that
    upgrading never changes how an existing deployment places orders.

### `pair`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `base_currency` | string | Yes | Base currency symbol (e.g., `SOL`, `ETH`, `BTC`). |
| `quote_currency` | string | Yes | Quote currency symbol (e.g., `USDT`, `USDC`). |

### `trading_settings`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `timeframe` | string | Yes | OHLCV timeframe (e.g., `1m`, `5m`, `1h`, `1d`). |
| `period.start_date` | string | Yes | Start date in ISO 8601 format. |
| `period.end_date` | string | Yes | End date in ISO 8601 format. |
| `initial_balance` | float | Yes | Starting balance in quote currency. |
| `historical_data_file` | string | No | Path to local CSV file for offline backtesting. If omitted, data is fetched via CCXT. |

### `grid_strategy`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `type` | string | Yes | `simple_grid` (lower half buys, upper half sells) or `hedged_grid` (classic grid with one free level next to the price). |
| `spacing` | string | Yes | `arithmetic` (equal intervals) or `geometric` (proportional intervals). |
| `num_grids` | int | Yes | Total number of grid levels. |
| `range.top` | float | Yes | Upper price limit of the grid. |
| `range.bottom` | float | Yes | Lower price limit of the grid. |
| `buy_ratio` | float | No | Fraction of the base quantity to use for buy orders. Must be in `(0, 1.0]`. Default: `1.0`. |
| `sell_ratio` | float | No | Fraction of the base quantity to use for sell orders. Must be in `(0, 1.0]`. Default: `1.0`. |

### `risk_management`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `take_profit.enabled` | bool | Yes | Whether take-profit is active. |
| `take_profit.threshold` | float | Yes | Price at which to take profit. |
| `stop_loss.enabled` | bool | Yes | Whether stop-loss is active. |
| `stop_loss.threshold` | float | Yes | Price at which to stop loss. |

### `execution` *(optional)*

All fields are optional with sensible defaults. These fine-tune order execution behavior.

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `max_retries` | int | `3` | 1–20 | Maximum attempts to cancel an order (live/paper). Placements are not retried here: a placement that failed for a transient reason is retried by the placement journal at every order poll. |
| `retry_delay` | float | `1.0` | 0.1–60.0 | Delay in seconds between cancellation attempts. |
| `max_slippage` | float | `0.01` | 0.0001–0.1 | Accepted for compatibility, no longer used: a market order is sent as one leg, and the initial purchase and the liquidation retry it without adjusting a price. |
| `backtest_slippage` | float | `0.0` | 0.0–0.1 | Fixed slippage applied to simulated fills (e.g., `0.001` = 0.1%). Buys fill higher, sells fill lower. |
| `backtest_min_amount` | float | none | 0 or more | Minimum order quantity in the base currency (backtest only). When set, the backtest applies it like the exchange's minimum in live: it checks that the grid is feasible at arming, skips the initial purchase and grid orders that fall below it, and skips the final liquidation if the position is too small. Absent means no minimum. |
| `backtest_min_cost` | float | none | 0 or more | Minimum order value in the quote currency (backtest only). Applied the same way as `backtest_min_amount`. Absent means no minimum. |
| `order_polling_interval` | float | `15.0` | 1.0–300.0 | Seconds between open order status polls (live/paper). |
| `websocket_max_retries` | int | `5` | 1–50 | Maximum WebSocket reconnection attempts. |
| `websocket_retry_base_delay` | int | `5` | 1–120 | Base delay (seconds) for WebSocket reconnection backoff. |
| `health_check_interval` | int | `60` | 10–3600 | Seconds between health check pings. |
| `circuit_breaker_failure_threshold` | int | `5` | 1–50 | Consecutive API failures before circuit breaker opens. |
| `circuit_breaker_recovery_timeout` | float | `60.0` | 1.0–600.0 | Seconds to wait before recovery attempt after circuit breaker opens. |
| `circuit_breaker_half_open_max_calls` | int | `1` | 1–10 | Maximum test calls allowed in half-open state. |
| `reconciliation_interval` | float | `300.0` | 60.0–3600.0 | Seconds between reconciliation cycles that audit local state vs exchange (live mode only). |
| `reconciliation_balance_tolerance` | float | `0.01` | 0.0–100.0 | Balance drift tolerated before an alert, as a value in the quote currency. The crypto check converts it at the last price and never goes below the exchange's minimum order amount. |

`reconciliation_interval` and `reconciliation_balance_tolerance` are checked against their ranges at start, like the other `execution` fields; a value outside them refuses the config (exit 78).

### `logging`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `log_level` | string | Yes | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `log_to_file` | bool | Yes | Enable logging to a file in the `logs/` directory. |

### `persistence` *(optional, live and paper trading)*

SQLite state persistence for crash recovery. The state is saved in `live` and `paper_trading` modes and ignored in `backtest`.

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `enabled` | bool | `true` | `true` or `false` | Whether to persist bot state to SQLite. A string such as `"false"` is refused. |
| `db_path` | string | `data/{BASE}_{QUOTE}/state_{hash}.db` | non-empty | Path to the SQLite database file. The default path includes a short config hash so different grid configurations for the same pair use separate databases. |
| `checkpoint_interval_seconds` | float | `60.0` | 1.0–3600.0 | Seconds between periodic checkpoints, which save the state that changes between order operations (the trailing-stop ratchet). |
