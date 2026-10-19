"""
A ccxt.pro-shaped exchange for BTC/USDT whose orders, balances and executions live in a JSON file: they survive
between two bot processes, and tests fill, cancel or remove orders while no bot runs. Fees are taken in the currency
each fill receives.
"""

import asyncio
from collections.abc import Callable
import json
from pathlib import Path
import time
from typing import Any, ClassVar

import ccxt

PAIR = "BTC/USDT"
BASE = "BTC"
QUOTE = "USDT"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _execute(data: dict, side: str, quantity: float, price: float, from_locked: bool) -> tuple[float, str]:
    """Moves the funds of `quantity` executed at `price`, the fee taken in the currency the fill receives."""
    balances = data["balances"]
    source = "used" if from_locked else "free"
    if side == "buy":
        fee, currency = quantity * data["fee"], BASE
        balances[QUOTE][source] -= quantity * price
        balances[BASE]["free"] += quantity - fee
    else:
        fee, currency = quantity * price * data["fee"], QUOTE
        balances[BASE][source] -= quantity
        balances[QUOTE]["free"] += quantity * price - fee
    data["executions"].append(
        {"side": side, "quantity": quantity, "price": price, "fee": fee, "fee_currency": currency}
    )
    return fee, currency


def _report_fee(data: dict, order: dict, fee: float, currency: str) -> None:
    if data.get("report_fees"):
        previous = order["fee"]["cost"] if order["fee"] else 0.0
        order["fee"] = {"cost": previous + fee, "currency": currency}


def _move(data: dict, asset: str, amount: float, from_key: str, to_key: str) -> None:
    data["balances"][asset][from_key] -= amount
    data["balances"][asset][to_key] += amount


def _open(
    data: dict,
    order_type: str,
    side: str,
    amount: float,
    price: float,
    client_order_id: str | None = None,
) -> dict:
    order_id = str(data["next_id"])
    data["next_id"] += 1
    order = {
        "id": order_id,
        "clientOrderId": client_order_id,
        "status": "open",
        "type": order_type,
        "side": side,
        "price": price,
        "average": None,
        "amount": amount,
        "filled": 0.0,
        "remaining": amount,
        "timestamp": _now_ms(),
        "lastTradeTimestamp": None,
        "symbol": PAIR,
        "fee": None,
        "cost": 0.0,
    }
    if order_type == "market":
        market_price = data["price"]
        fee, currency = _execute(data, side, amount, market_price, from_locked=False)
        order.update(
            status="closed",
            price=market_price,
            average=market_price,
            filled=amount,
            remaining=0.0,
            cost=amount * market_price,
            lastTradeTimestamp=_now_ms(),
        )
        _report_fee(data, order, fee, currency)
    elif side == "buy":
        _move(data, QUOTE, amount * price, "free", "used")
    else:
        _move(data, BASE, amount, "free", "used")
    data["orders"][order_id] = order
    return order


def _cancel(data: dict, order: dict) -> None:
    if order["status"] != "open":
        return
    if order["side"] == "buy":
        _move(data, QUOTE, order["remaining"] * order["price"], "used", "free")
    else:
        _move(data, BASE, order["remaining"], "used", "free")
    order["status"] = "canceled"


class ExchangeState:
    """The JSON file the fake exchange works on, with the helpers tests use to act on it."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def create(
        cls,
        path: Path,
        price: float = 100.0,
        quote: float = 1000.0,
        base: float = 0.0,
        fee: float = 0.001,
        order_delay: float | None = None,
        fail_feed_after: int | None = None,
        report_fees: bool = False,
    ) -> "ExchangeState":
        state = cls(path)
        state._write(
            {
                "price": price,
                "fee": fee,
                "order_delay": order_delay,
                "fail_feed_after": fail_feed_after,
                "report_fees": report_fees,
                "failing": [],
                "next_id": 1,
                "orders": {},
                "executions": [],
                "balances": {QUOTE: {"free": quote, "used": 0.0}, BASE: {"free": base, "used": 0.0}},
            }
        )
        return state

    def read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text())

    def update(self, change: Callable[[dict[str, Any]], Any]) -> Any:
        data = self.read()
        result = change(data)
        self._write(data)
        return result

    def _write(self, data: dict[str, Any]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data))
        temporary.replace(self.path)

    def open_orders(self, side: str | None = None) -> list[dict]:
        return [o for o in self.read()["orders"].values() if o["status"] == "open" and side in (None, o["side"])]

    def order_at(self, price: float, side: str) -> dict:
        matches = [o for o in self.open_orders(side) if o["price"] == price]
        assert len(matches) == 1, f"expected one open {side} at {price}, found {matches}"
        return matches[0]

    def has_open(self, price: float, side: str) -> bool:
        return any(o["price"] == price for o in self.open_orders(side))

    def fill(self, order_id: str, quantity: float | None = None) -> None:
        def change(data):
            order = data["orders"][order_id]
            filled = order["remaining"] if quantity is None or quantity >= order["remaining"] else quantity
            fee, currency = _execute(data, order["side"], filled, order["price"], from_locked=True)
            _report_fee(data, order, fee, currency)
            if filled == order["remaining"]:
                order["filled"], order["remaining"], order["status"] = order["amount"], 0.0, "closed"
            else:
                order["filled"] += filled
                order["remaining"] = order["amount"] - order["filled"]
            order["average"] = order["price"]
            order["lastTradeTimestamp"] = _now_ms()

        self.update(change)

    def cancel(self, order_id: str) -> None:
        self.update(lambda data: _cancel(data, data["orders"][order_id]))

    def remove(self, order_id: str) -> None:
        """Cancels the order and forgets it, as an exchange that archived it."""

        def change(data):
            _cancel(data, data["orders"][order_id])
            del data["orders"][order_id]

        self.update(change)

    def add_open_order(self, side: str, price: float, amount: float) -> str:
        return self.update(lambda data: _open(data, "limit", side, amount, price)["id"])

    def fail(self, method: str) -> None:
        """Makes every later call to `method` raise a ccxt NetworkError."""
        self.update(lambda data: data["failing"].append(method))

    def totals(self) -> dict[str, float]:
        return {asset: amounts["free"] + amounts["used"] for asset, amounts in self.read()["balances"].items()}

    def executions(self) -> list[dict]:
        return self.read()["executions"]

    def fees_paid(self) -> float:
        """Every fee charged so far, valued in the quote currency at its fill price."""
        return sum(e["fee"] * e["price"] if e["fee_currency"] == BASE else e["fee"] for e in self.read()["executions"])


class FakeExchange:
    state_path: ClassVar[Path] = Path("exchange.json")
    calls_path: ClassVar[Path | None] = None
    tick_interval: ClassVar[float] = 0.05

    def __init__(self, _params: dict):
        self.urls = {"api": "https://fake.invalid"}
        self.markets = None
        self._ticks = 0
        self._state = ExchangeState(self.state_path)

    def _record(self, method: str, **details) -> None:
        if self.calls_path is not None:
            with self.calls_path.open("a") as calls:
                calls.write(json.dumps({"method": method, **details}) + "\n")

    @staticmethod
    def _raise_if_failing(data: dict, method: str) -> None:
        if method in data["failing"]:
            raise ccxt.NetworkError(f"scripted {method} failure")

    def set_sandbox_mode(self, _enabled: bool) -> None:
        pass

    async def load_markets(self):
        self._record("load_markets")
        self.markets = {PAIR: {"limits": {"amount": {"min": 0.0001}, "cost": {"min": 1.0}}}}
        return self.markets

    def amount_to_precision(self, _pair, amount):
        # Full precision, so the funds the bot reserves equal the orders it places.
        return str(amount)

    def price_to_precision(self, _pair, price):
        return f"{price:.2f}"

    async def fetch_balance(self):
        data = self._state.read()
        self._raise_if_failing(data, "fetch_balance")
        balances = data["balances"]
        return {
            "free": {asset: amounts["free"] for asset, amounts in balances.items()},
            "used": {asset: amounts["used"] for asset, amounts in balances.items()},
            "total": {asset: amounts["free"] + amounts["used"] for asset, amounts in balances.items()},
        }

    async def fetch_ticker(self, _pair):
        return {"last": self._state.read()["price"]}

    async def fetch_status(self):
        return {"status": "ok"}

    async def watch_ticker(self, _pair):
        await asyncio.sleep(self.tick_interval)
        self._ticks += 1
        data = self._state.read()
        if data["fail_feed_after"] is not None and self._ticks > data["fail_feed_after"]:
            raise ccxt.NetworkError("scripted feed loss")
        return {"last": data["price"]}

    async def create_order(self, pair, order_type, side, amount, price=None, params=None):
        client_order_id = (params or {}).get("clientOrderId")
        delay = self._state.read()["order_delay"]
        if delay:
            await asyncio.sleep(delay)
        order = self._state.update(
            lambda data: _open(
                data,
                order_type,
                side,
                float(amount),
                float(price) if price is not None else data["price"],
                client_order_id,
            )
        )
        self._record("create_order", id=order["id"], type=order_type, side=side, client_order_id=client_order_id)
        return dict(order)

    async def fetch_order(self, order_id, _pair, params=None):
        data = self._state.read()
        self._raise_if_failing(data, "fetch_order")
        client_order_id = (params or {}).get("clientOrderId")
        if order_id is None and client_order_id is not None:
            for order in data["orders"].values():
                if order["clientOrderId"] == client_order_id:
                    return dict(order)
            raise ccxt.OrderNotFound(f"fake exchange: no order with client order id {client_order_id}")
        if order_id not in data["orders"]:
            raise ccxt.OrderNotFound(f"fake exchange: order {order_id} does not exist")
        return dict(data["orders"][order_id])

    async def cancel_order(self, order_id, _pair):
        def change(data):
            _cancel(data, data["orders"][order_id])
            return dict(data["orders"][order_id])

        order = self._state.update(change)
        self._record("cancel_order", id=order_id)
        return order

    async def fetch_open_orders(self, _pair):
        data = self._state.read()
        self._raise_if_failing(data, "fetch_open_orders")
        return [dict(order) for order in data["orders"].values() if order["status"] == "open"]

    async def fetch_ohlcv(self, *_args, **_kwargs):
        return []

    async def close(self):
        self._record("close")
