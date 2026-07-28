# Going Live

This page walks through moving a strategy from backtesting to real (or paper) money, and documents exactly what the bot does when it starts, stops, crashes, or restarts in live/paper trading mode.

A ready-to-use starting point is provided at [`config/config.live.example.json`](https://github.com/jordantete/grid_trading_bot/blob/master/config/config.live.example.json). It targets `paper_trading` mode with persistence, dynamic ATR-based grid spacing, and an ATR trailing stop already enabled.

!!! warning "Use `simple_grid` for live/paper trading"
    Stick with `grid_strategy.type: "simple_grid"` for live and paper trading. `hedged_grid` has known unfixed edge cases around order cancellation that can leave paired grid levels in an inconsistent state — it is not yet hardened for real capital.

## 1. Prerequisites

### API credentials

Live and paper trading both need exchange API credentials, provided via environment variables in a `.env` file at the repository root (loaded automatically via `python-dotenv`):

```bash
EXCHANGE_API_KEY=YourExchangeAPIKeyHere
EXCHANGE_SECRET_KEY=YourExchangeSecretKeyHere
```

See the [Environment Variables](../configuration/environment-variables.md) page for the full list, including the optional `APPRISE_NOTIFICATION_URLS` for alerting.

To create Binance API keys:

1. Log into your Binance account and go to API Management.
2. Create a new API key with **trading permissions**.
3. Copy the key and secret into your `.env` file.

!!! tip "Use the testnet first"
    Binance provides a free testnet with fake funds at [testnet.binance.vision](https://testnet.binance.vision). Create a separate API key/secret pair there and use them for `paper_trading` mode — the bot automatically points sandbox-capable exchanges (including Binance) at their testnet REST endpoint when `trading_mode` is `paper_trading`.

## 2. Recommended path

Don't jump straight to live capital. The recommended progression is:

1. **Backtest** (`trading_mode: "backtest"`) — validate the strategy against historical OHLCV data.
2. **Parameter sweep** — compare grid spacing, `num_grids`, and risk settings across multiple backtest runs.
3. **Paper trading on the testnet** (`trading_mode: "paper_trading"`) — run against real-time market data and Binance's sandbox endpoints, with no real funds at risk. This is what [`config/config.live.example.json`](https://github.com/jordantete/grid_trading_bot/blob/master/config/config.live.example.json) is set up for out of the box.
4. **Live with small capital** — once paper trading has run cleanly through several days of real market conditions, switch `exchange.trading_mode` to `"live"`, point `.env` at your production API keys, and start with an amount you can afford to lose.

```bash
uv run grid_trading_bot run --config config/config.live.example.json
```

## 3. What the bot does at startup (live)

1. **Loads exchange markets** for the configured pair via `load_exchange_markets`, then reads back the exchange's minimum order amount/cost constraints.
2. **Validates grid feasibility** against those constraints as soon as price first enters the grid range. If any grid level would produce an order below the exchange's minimum amount or minimum notional, the bot logs a clear error and stops itself (`STOP_BOT`) rather than placing an order the exchange would reject. Fix this by increasing `initial_balance`/capital or reducing `num_grids`.
3. **Attempts state recovery** (live mode with persistence enabled) from `data/{BASE}_{QUOTE}/state_{hash}.db` — see [Crash / restart behavior](#5-crash-restart-behavior) below.
4. If recovery is disabled, finds no prior state, or fails, the bot **falls back to a clean fresh start**: the grid and order book are reset, and the crypto/fiat balances currently held on the exchange account are treated as the starting capital.

## 4. Stopping

The bot distinguishes between a manual stop and a risk-triggered stop:

| Trigger | Behavior |
|---------|----------|
| Manual stop — `stop` command in the BotController, or SIGINT (Ctrl+C) | Cancels every open order on the exchange and releases their reserved funds. **The position is kept.** |
| Take-profit, stop-loss, or trailing stop (`on_trigger: "stop"`) | Cancels every open order, then **liquidates the full crypto position at market**. |

Both paths go through the same shutdown routine and are idempotent — a stop command received after a TP/SL liquidation has already run is a no-op.

!!! note "Restart vs. quit"
    The `restart` command stops and immediately restarts trading (position and connections are re-established). `quit` stops the bot and exits the process entirely. See the [CLI reference](cli.md#runtime-commands) for the full command list.

## 5. Crash / restart behavior

When `persistence.enabled` is `true` (the default) and `trading_mode` is `live`, the bot checkpoints its state to SQLite:

- On every order fill or cancellation event.
- On every regrid (volatility-triggered or trailing-stop-triggered).
- On shutdown.
- Every `persistence.checkpoint_interval_seconds` (default `60`) via a periodic timer.

On restart, the bot loads the saved state, reconciles it against the exchange (open orders, fills that happened while it was down, balances), and resumes from there — re-placing paired orders for any grid level that filled while the process was offline.

**A config change to the `grid_strategy`, `exchange` (name/trading_mode), `risk_management`, `pair`, or `trading_settings.timeframe` sections invalidates the saved state.** The bot detects this via a config hash and falls back to a fresh start rather than resuming with stale assumptions — this is deliberate: a fresh start with the *current* exchange balance as starting capital is safer than replaying old grid geometry against a changed configuration.

If recovery itself fails (corrupted state, unreachable exchange during reconciliation, etc.), the bot resets its in-memory state and — same as a fresh start — treats whatever position currently sits on the exchange account as the starting capital.

## 6. Monitoring

### Health checks

A background health check runs every `execution.health_check_interval` seconds (default `60`) and evaluates:

- **Strategy alive** — the trading loop is still running.
- **Exchange status** — the exchange's own status endpoint.
- **Ticker feed freshness** (live/paper only) — alerts if no accepted price update has been received for over 120 seconds.

If the WebSocket ticker feed dies and exhausts `execution.websocket_max_retries` reconnect attempts, the bot treats this as fatal: it cancels all open orders and stops cleanly rather than trading blind on a stale price.

### Notifications

Configure `APPRISE_NOTIFICATION_URLS` to receive alerts (order fills, errors, health check issues, TP/SL triggers) on Telegram, Discord, Slack, and more. See the [Notifications](notifications.md) page.

### Grafana / Loki

For log-based dashboards, start the monitoring stack:

```bash
docker-compose up -d
```

See [Monitoring Setup](../monitoring/setup.md) and [Grafana Dashboard](../monitoring/grafana-dashboard.md) for details.

## 7. Known limits

- **Order fills are detected via REST polling** (`execution.order_polling_interval`, default 15s) — there is no WebSocket order stream. Fills can take up to one polling interval to be detected.
- **Liquidation below the exchange's minimum notional is skipped.** If the remaining crypto position is too small to sell at market (dust), it is left on the exchange account rather than causing the shutdown to fail; a notification is sent instead.
