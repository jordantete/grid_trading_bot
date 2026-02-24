# Environment Variables

Sensitive credentials and service configuration are stored in a `.env` file at the repository root. This file is loaded automatically at startup.

!!! warning "Security"
    Never commit your `.env` file to version control. It is already listed in `.gitignore`.

## Example `.env` File

```bash
# Exchange API credentials
EXCHANGE_API_KEY=YourExchangeAPIKeyHere
EXCHANGE_SECRET_KEY=YourExchangeSecretKeyHere

# Notification URLs for Apprise
APPRISE_NOTIFICATION_URLS=

# Grafana Admin Access
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=YourGrafanaPasswordHere
```

## Variable Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `EXCHANGE_API_KEY` | Live/Paper only | Your API key for the exchange. Not needed for backtesting. |
| `EXCHANGE_SECRET_KEY` | Live/Paper only | Your secret key for the exchange. Not needed for backtesting. |
| `APPRISE_NOTIFICATION_URLS` | No | Comma-separated notification URLs (Telegram, Discord, Slack, etc.). See [Apprise documentation](https://github.com/caronc/apprise) for supported services. |
| `GRAFANA_ADMIN_USER` | No | Admin username for the Grafana monitoring stack. Default: `admin`. |
| `GRAFANA_ADMIN_PASSWORD` | No | Admin password for the Grafana monitoring stack. |

## Exchange API Keys

To obtain API keys for your exchange:

1. Log into your exchange account
2. Navigate to API Management settings
3. Create a new API key with **trading permissions**
4. Copy the API key and secret into your `.env` file

!!! tip "Paper Trading"
    For paper trading mode, use your exchange's **testnet/sandbox** API keys. Most exchanges provide separate sandbox environments for testing.

## Notification Setup

The bot uses [Apprise](https://github.com/caronc/apprise) for notifications. You can configure multiple notification channels by providing their URLs:

```bash
# Telegram example
APPRISE_NOTIFICATION_URLS=tgram://bot_token/chat_id

# Multiple channels (comma-separated)
APPRISE_NOTIFICATION_URLS=tgram://bot_token/chat_id,discord://webhook_id/webhook_token
```

See the [Apprise GitHub repository](https://github.com/caronc/apprise) for the full list of supported notification services.
