"""Runs the grid_trading_bot CLI against the persistent fake exchange, logging every exchange call to a JSONL file."""

import os
from pathlib import Path
import sys
from types import SimpleNamespace

from grid_trading_bot import cli
from grid_trading_bot.core.order_handling.execution_strategy.live_order_execution_strategy import (
    LiveOrderExecutionStrategy,
)
from grid_trading_bot.core.order_handling.liquidation import Liquidation
from grid_trading_bot.core.order_handling.order_status_tracker import OrderStatusTracker
from grid_trading_bot.core.services import live_exchange_service
from grid_trading_bot.strategies.live_grid_strategy import LiveGridStrategy
from tests.fakes.fake_exchange import FakeExchange


def _fail_tracker_polls_after(count: int) -> None:
    """Makes every order poll after the first `count` raise, to drive the supervised loop to its CRASH stop."""
    real_process = OrderStatusTracker._process_open_orders
    polls = 0

    async def process(self):
        nonlocal polls
        polls += 1
        if polls > count:
            raise RuntimeError("poll failure injected by the lifecycle harness")
        await real_process(self)

    OrderStatusTracker._process_open_orders = process


def main() -> None:
    os.environ.setdefault("APPRISE_NOTIFICATION_URLS", "")
    FakeExchange.state_path = Path(sys.argv[1])
    FakeExchange.calls_path = Path(sys.argv[2])
    assert hasattr(live_exchange_service, "ccxtpro")
    live_exchange_service.ccxtpro = SimpleNamespace(binance=FakeExchange)
    LiveGridStrategy.TICKER_REFRESH_INTERVAL = 0.2
    LiveOrderExecutionStrategy.LOOKUP_DELAY = 0.05
    Liquidation.LIQUIDATION_BACKOFF = (0.2,)
    if "TRACKER_POLLS_BEFORE_FAILING" in os.environ:
        _fail_tracker_polls_after(int(os.environ["TRACKER_POLLS_BEFORE_FAILING"]))
    cli.main(args=sys.argv[3:], prog_name="grid_trading_bot")


if __name__ == "__main__":
    main()
