# Notifications

The bot uses [Apprise](https://github.com/caronc/apprise) to send real-time notifications through services like Telegram, Discord, Slack, and [many more](https://github.com/caronc/apprise/wiki#notification-services).

!!! info
    Notifications are only active in **live** and **paper trading** modes. They are disabled during backtesting.

## Setup

Set the `APPRISE_NOTIFICATION_URLS` environment variable in your `.env` file:

```bash
# Single channel
APPRISE_NOTIFICATION_URLS=tgram://bot_token/chat_id

# Multiple channels (comma-separated)
APPRISE_NOTIFICATION_URLS=tgram://bot_token/chat_id,discord://webhook_id/webhook_token
```

In live and paper trading the bot logs a WARNING at start when the variable is empty, and an ERROR when a notification is not delivered.

See the [Environment Variables](../configuration/environment-variables.md) page for more details on `.env` configuration.

## Notification Types

### Order Notifications

| Type | Title | Trigger |
|------|-------|---------|
| `ORDER_FILLED` | Order Filled | After each fill, sent by `OrderManager` once its lock is released |
| `ORDER_PLACED` | Order Placement Successful | When an order is successfully placed |
| `ORDER_PENDING` | Order Placement Pending | Once per placement, at its first transient failure (network error, exchange unavailable); the bot retries it at every order poll |
| `ORDER_FAILED` | Order Placement Failed | When a placement is refused (insufficient balance, under the exchange minimum, refused by the exchange), and once per grid placement round that leaves levels unfunded |
| `ORDER_CANCELLED` | Order Cancellation | When an order is cancelled |

### Strategy Notifications

| Type | Title | Trigger |
|------|-------|---------|
| `TAKE_PROFIT_TRIGGERED` | Take Profit Triggered | When the take-profit price target is hit |
| `STOP_LOSS_TRIGGERED` | Stop Loss Triggered | When the stop-loss price target is hit |

### System Notifications

| Type | Title | Trigger |
|------|-------|---------|
| `ERROR_OCCURRED` | Error Occurred | When an unexpected error occurs; when a liquidation fails (first failure, then every 10 minutes), stops on a remainder under the exchange minimum or on an exchange holding less free crypto than the books, or is interrupted by the shutdown; when an open order carrying the bot's `clientOrderId` prefix is unknown at restart; when a liquidation was in progress at restart (the position may still be open, the bot halts) |
| `HEALTH_CHECK_ALERT` | Health Check Alert | When the health check detects an issue |
| `STATE_RECOVERY_COMPLETE` | State Recovery Complete | After a successful state recovery on restart; lists the rejected and ghost orders that were not re-placed |
| `BOT_HALTED` | Bot Halted | When the bot halts. The text asks you to check the exchange for orders left open before running `resume` |
| `BOT_SUSPENDED` | Bot Suspended | When the bot stops with its open orders kept; the next start resumes them |
| `START_REFUSED_HALTED` | Start Refused | Once, when a start is refused because the config is halted |
| `START_REFUSED` | Start Refused | When a start is refused for lack of a safe state: open orders on the pair with no saved state (the message lists their identifiers), or a saved state that cannot be resumed (written for another grid, or recovery failed). Exit 0, database untouched |
| `START_DEFERRED` | Start Deferred | When the exchange is unreachable while the saved state is loaded or the open orders are checked. Exit 75, so the supervisor relaunches the bot |
| `CHECKPOINT_FAILED` | Checkpoint Failed | At the first failed write of the state database; trading continues. After 3 failures in a row the bot halts |
| `CHECKPOINT_RESTORED` | Checkpoint Restored | At the first successful write after one or more failures |

### Reconciliation Notifications

| Type | Title | Trigger |
|------|-------|---------|
| `RECONCILIATION_ORDER_MISMATCH` | Order Reconciliation Mismatch | When local orders don't match exchange orders |
| `RECONCILIATION_BALANCE_DRIFT` | Balance Reconciliation Drift | When tracked balances drift from exchange balances |

## Supported Services

Apprise supports 100+ notification services. Common examples:

| Service | URL Format |
|---------|------------|
| Telegram | `tgram://bot_token/chat_id` |
| Discord | `discord://webhook_id/webhook_token` |
| Slack | `slack://token_a/token_b/token_c/#channel` |
| Email (SMTP) | `mailto://user:pass@gmail.com` |
| Pushover | `pover://user_key@app_token` |

For the full list, see the [Apprise Wiki](https://github.com/caronc/apprise/wiki#notification-services).
