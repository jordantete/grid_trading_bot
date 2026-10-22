"""
A ccxt.pro-shaped exchange for BTC/USDT whose orders, balances and executions live in a JSON file: they survive
between two bot processes, and tests fill, cancel or remove orders while no bot runs. Fees are taken in the currency
each fill receives.
"""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import time
from typing import Any, ClassVar

import ccxt

PAIR = "BTC/USDT"
BASE = "BTC"
QUOTE = "USDT"

_ERRORS = {
    "network": ccxt.NetworkError,
    "timeout": ccxt.RequestTimeout,
    "insufficient_funds": ccxt.InsufficientFunds,
    "duplicate": ccxt.DuplicateOrderId,
}


def _take_failure(data: dict, method: str) -> dict | None:
    """Consumes one scripted failure of `method`, dropping it once its count is spent."""
    failure = data["failures"].get(method)
    if failure is None:
        return None
    if failure["times"] is not None:
        failure["times"] -= 1
        if failure["times"] <= 0:
            del data["failures"][method]
    return dict(failure)


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
        if data.get("malformed_market_fee"):
            order["fee"] = {"cost": "n/a", "currency": currency}
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


def _fill(data: dict, order_id: str, quantity: float | None) -> None:
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
        response_delay: float | None = None,
        cancel_delay: float | None = None,
        report_fees: bool = False,
        malformed_market_fee: bool = False,
    ) -> "ExchangeState":
        state = cls(path)
        state._write(
            {
                "price": price,
                "fee": fee,
                "order_delay": order_delay,
                "fail_feed_after": fail_feed_after,
                "report_fees": report_fees,
                "malformed_market_fee": malformed_market_fee,
                "failures": {},
                "response_delay": response_delay,
                "cancel_delay": cancel_delay,
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
        """Reads, changes and writes the file under an exclusive lock, so the bot and the test never lose a write."""
        with self._locked():
            data = self.read()
            result = change(data)
            self._write(data)
        return result

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

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
        self.update(lambda data: _fill(data, order_id, quantity))

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

    _SCENARIO_FIELDS = frozenset({"price", "order_delay", "response_delay", "cancel_delay", "malformed_market_fee"})

    def fail(self, method: str, times: int | None = None, error: str = "network", after_accept: bool = False) -> None:
        """
        Makes the next `times` calls to `method` raise the ccxt error named by `error`, every later call when `times`
        is None. With `after_accept`, `create_order` creates the order before raising, as an answer lost on the way.
        """
        if error not in _ERRORS:
            raise ValueError(f"unknown error {error!r}, expected one of {sorted(_ERRORS)}")
        failure = {"times": times, "error": error, "after_accept": after_accept}
        self.update(lambda data: data["failures"].__setitem__(method, failure))

    def set_scenario(self, **fields) -> None:
        """Changes fields a running bot reads at every call, such as the price of the next tick."""
        unknown = set(fields) - self._SCENARIO_FIELDS
        if unknown:
            raise ValueError(f"not settable: {sorted(unknown)}")
        self.update(lambda data: data.update(fields))

    def repair_market_fees(self) -> None:
        """Stops writing malformed fees and clears those already on the orders."""

        def change(data):
            data["malformed_market_fee"] = False
            for order in data["orders"].values():
                if order["fee"] is not None and order["fee"].get("cost") == "n/a":
                    order["fee"] = None

        self.update(change)

    def fill_all(self, order_ids: list[str]) -> None:
        """Fills several open orders in one write, so a single poll of the bot sees them all filled."""

        def change(data):
            for order_id in order_ids:
                _fill(data, order_id, None)

        self.update(change)

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
    has: ClassVar[dict[str, Any]] = {"sandbox": True}

    def __init__(self, _params: dict):
        self.urls = {"api": "https://fake.invalid"}
        self.markets = None
        self._ticks = 0
        self._state = ExchangeState(self.state_path)

    def _record(self, method: str, **details) -> None:
        if self.calls_path is not None:
            with self.calls_path.open("a") as calls:
                calls.write(json.dumps({"method": method, **details}) + "\n")

    def _failure(self, method: str) -> dict | None:
        """The scripted failure of this call to `method`, consumed from the exchange file; None when it succeeds."""
        if method not in self._state.read()["failures"]:
            return None
        return self._state.update(lambda data: _take_failure(data, method))

    @staticmethod
    def _error(failure: dict, method: str) -> Exception:
        return _ERRORS[failure["error"]](f"scripted {method} failure")

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
        failure = self._failure("fetch_balance")
        if failure is not None:
            raise self._error(failure, "fetch_balance")
        balances = self._state.read()["balances"]
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
        failure = self._failure("create_order")
        if failure is not None and not failure["after_accept"]:
            self._record(
                "create_order_failed",
                type=order_type,
                side=side,
                client_order_id=client_order_id,
                error=failure["error"],
            )
            raise self._error(failure, "create_order")

        def place(data):
            if client_order_id is not None and any(
                o.get("clientOrderId") == client_order_id and o["status"] == "open" for o in data["orders"].values()
            ):
                return None
            price_used = float(price) if price is not None else data["price"]
            order = _open(data, order_type, side, float(amount), price_used, client_order_id)
            return dict(order)

        order = self._state.update(place)
        if order is None:
            self._record(
                "create_order_failed", type=order_type, side=side, client_order_id=client_order_id, error="duplicate"
            )
            raise ccxt.DuplicateOrderId(f"fake exchange: an open order already has client id {client_order_id}")
        self._record(
            "create_order",
            id=order["id"],
            type=order_type,
            side=side,
            price=order["price"],
            client_order_id=client_order_id,
            answer_lost=failure is not None,
        )
        if failure is not None:
            raise self._error(failure, "create_order")
        response_delay = self._state.read()["response_delay"]
        if response_delay:
            await asyncio.sleep(response_delay)
        return order

    async def fetch_order(self, order_id, _pair, params=None):
        failure = self._failure("fetch_order")
        if failure is not None:
            raise self._error(failure, "fetch_order")
        orders = self._state.read()["orders"]
        if order_id is None:
            client_order_id = (params or {}).get("clientOrderId")
            if client_order_id is None:
                raise ccxt.OrderNotFound("fake exchange: a lookup needs an order id or a client id")
            matches = [order for order in orders.values() if order.get("clientOrderId") == client_order_id]
            self._record("fetch_order_by_client_id", client_order_id=client_order_id, found=bool(matches))
            if not matches:
                raise ccxt.OrderNotFound(f"fake exchange: no order with client id {client_order_id}")
            return dict(matches[-1])
        if order_id not in orders:
            raise ccxt.OrderNotFound(f"fake exchange: order {order_id} does not exist")
        return dict(orders[order_id])

    async def cancel_order(self, order_id, _pair):
        delay = self._state.read()["cancel_delay"]
        if delay:
            await asyncio.sleep(delay)
        failure = self._failure("cancel_order")
        if failure is not None:
            raise self._error(failure, "cancel_order")

        def change(data):
            _cancel(data, data["orders"][order_id])
            return dict(data["orders"][order_id])

        order = self._state.update(change)
        self._record("cancel_order", id=order_id)
        return order

    async def fetch_open_orders(self, _pair):
        failure = self._failure("fetch_open_orders")
        if failure is not None:
            raise self._error(failure, "fetch_open_orders")
        return [dict(order) for order in self._state.read()["orders"].values() if order["status"] == "open"]

    async def fetch_ohlcv(self, *_args, **_kwargs):
        return []

    async def close(self):
        self._record("close")
