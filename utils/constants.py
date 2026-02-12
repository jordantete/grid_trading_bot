CANDLE_LIMITS = {
    "binance": 1000,
    "coinbase": 300,
    "kraken": 720,
    "bitfinex": 5000,
    "bitstamp": 1000,
    "huobi": 2000,
    "okex": 1440,
    "bybit": 200,
    "bittrex": 500,
    "poloniex": 500,
    "gateio": 1000,
    "kucoin": 1500,
}

TIMEFRAME_MAPPINGS = {
    "1s": 1 * 1000,  # 1 second
    "1m": 60 * 1000,  # 1 minute
    "3m": 3 * 60 * 1000,  # 3 minutes
    "5m": 5 * 60 * 1000,  # 5 minutes
    "15m": 15 * 60 * 1000,  # 15 minutes
    "30m": 30 * 60 * 1000,  # 30 minutes
    "1h": 60 * 60 * 1000,  # 1 hour
    "2h": 2 * 60 * 60 * 1000,  # 2 hours
    "6h": 6 * 60 * 60 * 1000,  # 6 hours
    "12h": 12 * 60 * 60 * 1000,  # 12 hours
    "1d": 24 * 60 * 60 * 1000,  # 1 day
    "3d": 3 * 24 * 60 * 60 * 1000,  # 3 days
    "1w": 7 * 24 * 60 * 60 * 1000,  # 1 week
    "1M": 30 * 24 * 60 * 60 * 1000,  # 1 month (approximated as 30 days)
}

RESOURCE_THRESHOLDS = {
    "cpu": 90,
    "bot_cpu": 80,
    "memory": 80,
    "bot_memory": 70,
    "disk": 90,
}
