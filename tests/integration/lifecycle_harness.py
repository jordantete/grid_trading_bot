"""Runs the grid_trading_bot CLI against a scripted in-memory exchange that logs every call to a JSONL file."""

import asyncio
import itertools
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import ClassVar

import ccxt

from grid_trading_bot import cli
from grid_trading_bot.core.services import live_exchange_service
from grid_trading_bot.strategies.grid_trading_strategy import GridTradingStrategy

PAIR = "BTC/USDT"


class FakeExchange:
    scenario: ClassVar[dict] = {}
    calls_path: ClassVar[Path] = Path("calls.jsonl")

    def __init__(self, _params: dict):
        self.urls = {"api": "https://fake.invalid"}
        self.markets = None
        self._orders: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._ticks = 0
        self._price = float(self.scenario.get("price", 100.0))

    def _record(self, method: str, **details) -> None:
        with self.calls_path.open("a") as calls:
            calls.write(json.dumps({"method": method, **details}) + "\n")

    def set_sandbox_mode(self, _enabled: bool) -> None:
        pass

    async def load_markets(self):
        self._record("load_markets")
        self.markets = {PAIR: {"limits": {"amount": {"min": 0.0001}, "cost": {"min": 1.0}}}}
        return self.markets

    def amount_to_precision(self, _pair, amount):
        return f"{amount:.6f}"

    def price_to_precision(self, _pair, price):
        return f"{price:.2f}"

    async def fetch_balance(self):
        return {
            "free": {"USDT": 1000.0, "BTC": 0.0},
            "used": {"USDT": 0.0, "BTC": 0.0},
            "total": {"USDT": 1000.0, "BTC": 0.0},
        }

    async def fetch_ticker(self, _pair):
        return {"last": self._price}

    async def fetch_status(self):
        return {"status": "ok"}

    async def watch_ticker(self, _pair):
        await asyncio.sleep(0.05)
        self._ticks += 1
        fail_after = self.scenario.get("fail_feed_after")
        if fail_after is not None and self._ticks > fail_after:
            raise ccxt.NetworkError("scripted feed loss")
        return {"last": self._price}

    async def create_order(self, pair, order_type, side, amount, price=None, params=None):
        order_id = str(next(self._ids))
        is_market = order_type == "market"
        order = {
            "id": order_id,
            "status": "closed" if is_market else "open",
            "type": order_type,
            "side": side,
            "price": price if price is not None else self._price,
            "average": self._price if is_market else None,
            "amount": amount,
            "filled": amount if is_market else 0.0,
            "remaining": 0.0 if is_market else amount,
            "timestamp": int(time.time() * 1000),
            "symbol": pair,
            "fee": None,
            "cost": amount * self._price if is_market else 0.0,
        }
        self._orders[order_id] = order
        self._record("create_order", id=order_id, type=order_type, side=side)
        return dict(order)

    async def fetch_order(self, order_id, _pair):
        return dict(self._orders[order_id])

    async def cancel_order(self, order_id, _pair):
        self._orders[order_id]["status"] = "canceled"
        self._record("cancel_order", id=order_id)
        return dict(self._orders[order_id])

    async def fetch_open_orders(self, _pair):
        return [dict(order) for order in self._orders.values() if order["status"] == "open"]

    async def fetch_ohlcv(self, *_args, **_kwargs):
        return []

    async def close(self):
        self._record("close")


def main() -> None:
    FakeExchange.scenario = json.loads(Path(sys.argv[1]).read_text())
    FakeExchange.calls_path = Path(sys.argv[2])
    live_exchange_service.ccxtpro = SimpleNamespace(binance=FakeExchange)
    GridTradingStrategy.TICKER_REFRESH_INTERVAL = 0.2
    cli.main(args=sys.argv[3:], prog_name="grid_trading_bot")


if __name__ == "__main__":
    main()
