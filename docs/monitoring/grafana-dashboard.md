# Grafana Dashboard

A pre-built Grafana dashboard is included for real-time monitoring of bot activity.

## Import the Dashboard

The dashboard JSON is located at:

```
monitoring/dashboards/grid_trading_bot_dashboard.json
```

**To import:**

1. Open Grafana at [http://localhost:3000](http://localhost:3000)
2. Go to **Dashboards** > **Import**
3. Upload the JSON file or paste its contents
4. Select the **Loki** datasource when prompted

!!! tip "Auto-provisioning"
    If you started the stack with `docker-compose up -d`, the dashboard is automatically provisioned in the **TradingBot Monitoring** folder. No manual import needed.

**Dashboard UID:** `grid-trading-bot`
**Default refresh rate:** 10 seconds

## Template Variables

The dashboard includes three filter dropdowns:

| Variable | Source | Description |
|----------|--------|-------------|
| `trading_pair` | Loki label values | Filter by trading pair (e.g., `SOL/USDT`) |
| `trading_mode` | Loki label values | Filter by mode (backtest/paper_trading/live) |
| `strategy` | Loki label values (`strategy_type`) | Filter by strategy type |

## Panels

### 1. Price & Orders

A time-series chart showing:

- **Price line** — Current price extracted from bot logs
- **Buy orders** — Green points at the grid level of every buy order placed (initial, paired after a fill, re-placed after a cancel), logged by `GridManager`
- **Sell orders** — Red points at the grid level of every sell order placed (initial, paired after a fill, re-placed after a cancel), logged by `GridManager`

### 2. Balance History

A dual-axis time-series chart tracking:

- **Fiat balance** (left Y-axis) — Quote currency balance over time
- **Crypto balance** (right Y-axis) — Base currency balance over time
- **Total base balance** (right Y-axis, dashed yellow) — Total portfolio value in base currency (`crypto + fiat / price`)

Values are extracted from `BalanceTracker` logs.

### 3. System Health (CPU %)

A gauge panel showing current CPU usage:

| Range | Color | Meaning |
|-------|-------|---------|
| 0–70% | Green | Healthy |
| 70–85% | Yellow | Elevated |
| 85–100% | Red | Critical |

Data is extracted from `HealthCheck` logs.

### 4. Strategy Overview

A stat panel displaying the current strategy configuration:

- Grid size (number of levels)
- Grid price range
- Spacing type

### 5. Executed Trades

A stat panel showing cumulative trade counts:

- **Buy Fills** (green) — Total buy orders completed at grid levels
- **Sell Fills** (red) — Total sell orders completed at grid levels

Both counters use count_over_time over the selected time range on OrderManager's "order completed at grid level" lines.

### 6. Account Value

The account value in quote currency (fiat plus all crypto held, reserved included, at the last accepted price) and its peak, from the `Account metrics` line the health check logs every `execution.health_check_interval` seconds (default 60), with or without fills.

### 7. Drawdown

The fall from the peak, in percent. In live and paper trading the peak is persisted with the bot state and survives restarts.

### 8. Realized vs Unrealized PnL

Realized PnL is booked on every sell at the weighted average cost of the crypto held, fees included. Unrealized PnL is the crypto held times the gap between the current price and that cost. Their sum is the change in account value since the bot started (since the first price seen for crypto already on the account at start).

### 9. Realized PnL per Day, Week and Month

Growth of realized PnL over rolling windows of 24 hours, 7 days and 30 days, each bar ending at its own timestamp. These are not calendar days, weeks or months. The three panels keep their own time range (last 30 days, 12 weeks and 11 months) whatever range the dashboard shows.

Each run of the bot writes its own log file, so the bars are computed run by run: realized PnL booked between two runs (fills replayed after a restart, or the last minute before a crash) does not appear in any bar, and a paper-trading restart, which resets realized PnL, does not show as a negative bar.

## Customization

### Change Refresh Interval

Click the refresh icon in the top-right corner and select your preferred interval (default: 10s).

### Add Custom Panels

1. Click **Add panel** in the dashboard
2. Select **Loki** as the datasource
3. Write a LogQL query filtering on the available labels (`trading_pair`, `trading_mode`, `strategy_type`, etc.)

Every `|=` filter, `|~` alternative and `regexp` prefix in the dashboard must exist in the bot's source: `tests/monitoring/test_dashboard_log_patterns.py` fails otherwise, so a reworded log message cannot silently empty a panel.

### Useful LogQL Queries

```logql
# All errors for a specific trading pair
{trading_pair="SOL/USDT"} |= "ERROR"

# Order fills only
{trading_pair="SOL/USDT"} |= "order filled"

# Health check entries
{trading_pair="SOL/USDT"} |= "HealthCheck"
```
