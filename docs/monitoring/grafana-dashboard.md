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
- **Buy orders** — Green points marking buy limit order executions at grid level prices
- **Sell orders** — Red points marking sell limit order executions at grid level prices

### 2. Balance History

A dual-axis time-series chart tracking:

- **Fiat balance** (left Y-axis) — Quote currency balance over time
- **Crypto balance** (right Y-axis) — Base currency balance over time

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

## Customization

### Change Refresh Interval

Click the refresh icon in the top-right corner and select your preferred interval (default: 10s).

### Add Custom Panels

1. Click **Add panel** in the dashboard
2. Select **Loki** as the datasource
3. Write a LogQL query filtering on the available labels (`trading_pair`, `trading_mode`, `strategy_type`, etc.)

### Useful LogQL Queries

```logql
# All errors for a specific trading pair
{trading_pair="SOL/USDT"} |= "ERROR"

# Order fills only
{trading_pair="SOL/USDT"} |= "order filled"

# Health check entries
{trading_pair="SOL/USDT"} |= "HealthCheck"
```
