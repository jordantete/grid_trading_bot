"""Runs the grid_trading_bot CLI against the persistent fake exchange, logging every exchange call to a JSONL file."""

import os
from pathlib import Path
import sys
from types import SimpleNamespace

from grid_trading_bot import cli
from grid_trading_bot.core.order_handling.execution_strategy.live_order_execution_strategy import (
    LiveOrderExecutionStrategy,
)
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.services import live_exchange_service
from grid_trading_bot.strategies.grid_trading_strategy import GridTradingStrategy
from tests.fakes.fake_exchange import FakeExchange


def main() -> None:
    os.environ.setdefault("APPRISE_NOTIFICATION_URLS", "")
    FakeExchange.state_path = Path(sys.argv[1])
    FakeExchange.calls_path = Path(sys.argv[2])
    assert hasattr(live_exchange_service, "ccxtpro")
    live_exchange_service.ccxtpro = SimpleNamespace(binance=FakeExchange)
    GridTradingStrategy.TICKER_REFRESH_INTERVAL = 0.2
    LiveOrderExecutionStrategy.LOOKUP_DELAY = 0.05
    OrderManager.LIQUIDATION_BACKOFF = (0.2,)
    cli.main(args=sys.argv[3:], prog_name="grid_trading_bot")


if __name__ == "__main__":
    main()
