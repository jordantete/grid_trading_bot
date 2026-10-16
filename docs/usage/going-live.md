# Going Live

This page walks through moving a strategy from backtesting to real (or paper) money, and documents exactly what the bot does when it starts, stops, crashes, or restarts in live/paper trading mode.

A ready-to-use starting point is provided at [`config/config.live.example.json`](https://github.com/jordantete/grid_trading_bot/blob/master/config/config.live.example.json). It targets `paper_trading` mode with persistence, dynamic ATR-based grid spacing, and an ATR trailing stop already enabled.

!!! warning "Validate the strategy in paper trading first"
    Live trading stays on `simple_grid` until the fills of one poll are ordered like the backtest (tracked separately). Validate `hedged_grid` in paper trading first. Neither strategy has run with real capital yet, so go live with a small amount before scaling up.

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
2. **Validates grid feasibility** against those constraints as soon as price first enters the grid range. If any grid level would produce an order below the exchange's minimum amount or minimum notional, the bot logs a clear error, places nothing and halts (see [Stopping](#4-stopping)). Fix this by increasing `initial_balance`/capital or reducing `num_grids`.
3. **Attempts state recovery** (live mode with persistence enabled) from `data/{BASE}_{QUOTE}/state_{hash}.db` — see [Crash / restart behavior](#5-crash-restart-behavior) below.
4. If recovery is disabled, finds no prior state, or fails, the bot **falls back to a clean fresh start**: the grid and order book are reset, and the crypto/fiat balances currently held on the exchange account are treated as the starting capital.

## 4. Stopping

Every way the bot ends has a fixed effect on the open orders, the position and the exit code:

| Trigger | Open orders | Position | Halt recorded | Exit code |
|---------|-------------|----------|---------------|-----------|
| `stop` or `quit` command, or SIGUSR1 | Cancelled | Kept | Yes | 0 |
| Take-profit, stop-loss or trailing stop | Cancelled | Liquidated at market | Yes | 0 |
| Grid infeasible (order below the exchange minimum) | Nothing placed | None | Yes | 0 |
| SIGINT or SIGTERM (`docker stop`, reboot) | Kept | Kept | No | 130 (SIGINT) or 143 (SIGTERM) |
| Ticker feed lost after its retries | Kept | Kept | No | 75 |
| Unexpected exception | Kept | Kept | No | 1 |

A config error at start exits with 78 and nothing starts. Starting a config that another process already runs exits with 1.

Every path writes a final checkpoint of the state, once the saved state was loaded. A stop requested while the bot is still starting runs as soon as startup ends.

### Halts and `resume`

A halt means the bot decided to stop trading, so a relaunch must not start trading again by itself. The bot writes a marker file `halted_<hash8>.json` next to the state database. While the marker exists, starting the same config logs a WARNING, sends one "Start Refused" notification, makes no call to the exchange and exits with 0.

To trade again, check the exchange for orders left open (the halt notification asks for this check), then run:

```bash
uv run grid_trading_bot resume --config config/config.live.json
```

`resume` moves the state database (and its `-wal` and `-shm` files) aside as `<name>.halted-<UTC timestamp>` and deletes the marker. The next start is a fresh grid on the balances currently held on the exchange. Without a halt, `resume` changes nothing and says the next start resumes the saved state. It refuses with exit code 1 while a bot holds the config's lock.

A config change gives a new hash, so a new state database and no marker.

!!! note "Stop, quit and the old commands"
    `stop` and `quit` both halt the bot. `restart` and `pause` no longer exist. See the [CLI reference](cli.md#runtime-commands) for the command list.

### Running unattended (Docker)

```bash
# Start the bot (add --build after a code change)
docker compose --profile bot up -d bot

# Suspend: orders kept, exit 143, relaunched after a Docker daemon restart or a reboot
docker compose stop bot

# Suspend and keep it down across reboots: orders kept, container removed
docker compose down bot

# Halt: orders cancelled, exit 0, stays down
docker compose kill -s SIGUSR1 bot

# After a halt: clear it, then start a new grid
docker compose run --rm bot resume --config ${BOT_CONFIG:-config/config.json}
docker compose --profile bot up -d bot
```

The compose file sets `restart: on-failure`. A halted bot exits with 0 and stays down, while a crash, a lost feed or a reboot relaunches it. The container gets the default SIGTERM and a 30 second grace period, and has no `stdin_open`: the command listener only runs in an interactive terminal.

`on-failure` looks at the exit code only. A container stopped by `docker compose stop` exits with 143, so the Docker daemon relaunches it after a daemon restart or a host reboot. To keep a suspended bot down across reboots, remove it with `docker compose down bot`, or halt it with SIGUSR1. The manual Docker check run before go-live confirms this behavior.

The `resume` example passes `${BOT_CONFIG:-config/config.json}`, the same config file the service runs.

| Exit code | Meaning | Relaunched by `on-failure` |
|-----------|---------|----------------------------|
| 0 | Halt (or a refused start) | No |
| 1 | Unexpected exception, or the config is already running | Yes |
| 75 | Ticker feed lost | Yes |
| 78 | Config error | Yes, and fails again until the config is fixed |
| 130 | SIGINT | Yes |
| 143 | SIGTERM (`docker compose stop`, reboot) | After a Docker daemon restart or a host reboot, even when stopped by `docker compose stop` |

## 5. Crash / restart behavior

When `persistence.enabled` is `true` (the default) and `trading_mode` is `live`, the bot checkpoints its state to SQLite:

- On every order fill or cancellation event.
- On every regrid (volatility-triggered or trailing-stop-triggered).
- On shutdown.
- Every `persistence.checkpoint_interval_seconds` (default `60`) via a periodic timer.

A halted config refuses to start until [`resume`](#halts-and-resume) is run, and two processes cannot run the same config: the lock file `bot_<hash8>.lock` sits next to the state database.

On restart, the bot loads the saved state, reconciles it against the exchange (open orders, fills that happened while it was down, balances), and resumes from there — re-placing paired orders for any grid level that filled while the process was offline.

**A config change to the `grid_strategy`, `exchange` (name/trading_mode), `risk_management`, `pair`, or `trading_settings.timeframe` sections invalidates the saved state.** The bot detects this via a config hash and falls back to a fresh start rather than resuming with stale assumptions — this is deliberate: a fresh start with the *current* exchange balance as starting capital is safer than replaying old grid geometry against a changed configuration.

If recovery itself fails (corrupted state, unreachable exchange during reconciliation, etc.), the bot resets its in-memory state and — same as a fresh start — treats whatever position currently sits on the exchange account as the starting capital.

## 6. Monitoring

### Health checks

A background health check runs every `execution.health_check_interval` seconds (default `60`) and evaluates:

- **Strategy alive** — the trading loop is still running.
- **Exchange status** — the exchange's own status endpoint.
- **Ticker feed freshness** (live/paper only) — alerts if no accepted price update has been received for over 120 seconds.

If the WebSocket ticker feed dies and exhausts `execution.websocket_max_retries` reconnect attempts, the bot treats this as fatal: the bot keeps its open orders, writes a checkpoint, sends a notification and exits with code 75 so the supervisor relaunches it. Recovery reconciles the state with the exchange on restart.

### Notifications

Configure `APPRISE_NOTIFICATION_URLS` to receive alerts (order fills, errors, health check issues, TP/SL triggers, halts) on Telegram, Discord, Slack, and more. A WARNING is logged at start when the variable is empty, and an ERROR when a notification is not delivered. The compose file passes the variable to the bot container. See the [Notifications](notifications.md) page.

### Grafana / Loki

For log-based dashboards, start the monitoring stack:

```bash
docker compose up -d
```

The dashboard charts the account value, its peak, the drawdown and realized/unrealized PnL from a line the health check logs every interval, so the curve keeps moving when no order fills. Realized PnL uses the weighted average cost of the crypto held. A manual deposit or withdrawal on the exchange account changes the position without changing that cost, so after one the realized plus unrealized PnL no longer adds up to the change in account value. They also move the drawdown: a withdrawal shows as a lasting drawdown because the peak is persisted, and a deposit raises the peak. The peak only starts over on a fresh start (a config change or a new state database).

See [Monitoring Setup](../monitoring/setup.md) and [Grafana Dashboard](../monitoring/grafana-dashboard.md) for details.

## 7. Known limits

- **Order fills are detected via REST polling** (`execution.order_polling_interval`, default 15s) — there is no WebSocket order stream. Fills can take up to one polling interval to be detected.
- **Liquidation below the exchange's minimum notional is skipped.** If the remaining crypto position is too small to sell at market (dust), it is left on the exchange account rather than causing the shutdown to fail; a notification is sent instead.
