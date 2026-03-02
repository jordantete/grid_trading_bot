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

See the [Environment Variables](../configuration/environment-variables.md) page for more details on `.env` configuration.

## Notification Types

### Order Notifications

| Type | Title | Trigger |
|------|-------|---------|
| `ORDER_FILLED` | Order Filled | Automatically on every order fill (via EventBus) |
| `ORDER_PLACED` | Order Placement Successful | When an order is successfully placed |
| `ORDER_FAILED` | Order Placement Failed | When an order placement fails |
| `ORDER_CANCELLED` | Order Cancellation | When an order is cancelled |

!!! note
    `ORDER_FILLED` is the only notification automatically triggered via an EventBus subscription. All other notifications are sent programmatically by the relevant components.

### Strategy Notifications

| Type | Title | Trigger |
|------|-------|---------|
| `TAKE_PROFIT_TRIGGERED` | Take Profit Triggered | When the take-profit price target is hit |
| `STOP_LOSS_TRIGGERED` | Stop Loss Triggered | When the stop-loss price target is hit |

### System Notifications

| Type | Title | Trigger |
|------|-------|---------|
| `ERROR_OCCURRED` | Error Occurred | When an unexpected error occurs in the bot |
| `HEALTH_CHECK_ALERT` | Health Check Alert | When the health check detects an issue |
| `STATE_RECOVERY_COMPLETE` | State Recovery Complete | After a successful state recovery on restart |

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
