from collections import Counter
import dataclasses
import json
import math
from pathlib import Path
from typing import Any

import pytest

from grid_trading_bot.core.order_handling.order import OrderStatus, OrderType

# Filled by check_snapshot under --update-snapshots, printed by the integration conftest's terminal summary.
UPDATED_SNAPSHOTS: list[tuple[str, list[str]]] = []

_REL_TOL = 1e-9
_ABS_TOL = 1e-12


def _creation_rank(identifier: str) -> tuple[int, str]:
    """Backtest ids are `backtest-<counter>`: the counter gives the order the bot created them in."""
    prefix, _, counter = identifier.rpartition("-")
    return (int(counter), prefix) if counter.isdigit() else (-1, identifier)


def _fill_entry(order, balances) -> dict[str, Any]:
    fill_price = order.average if order.average is not None else order.price
    fee = balances.fee_calculator.fill_fee(order.side, order.filled, fill_price, order.liquidity)
    return {
        "identifier": order.identifier,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "price": order.price,
        "average": order.average,
        "filled": order.filled,
        "fee_cost": fee.amount,
        "fee_currency": balances.base_currency if fee.in_base else balances.quote_currency,
        # Market orders carry the wall-clock time of the run, not the candle's.
        "timestamp": None if order.order_type == OrderType.MARKET else order.timestamp,
    }


def extract_snapshot_data(bot, result: dict) -> dict[str, Any]:
    """The full-precision result of a backtest: typed metrics, final balances and every fill, in creation order."""
    balances = bot.balance_tracker
    orders = [*bot.order_book.get_all_buy_orders(), *bot.order_book.get_all_sell_orders()]
    orders.sort(key=lambda order: _creation_rank(order.identifier))
    return {
        "metrics": dataclasses.asdict(result["metrics"]),
        "balances": {
            "fiat": str(balances._balance),
            "crypto": str(balances._crypto_balance),
            "reserved_fiat": str(balances._reserved_fiat),
            "reserved_crypto": str(balances._reserved_crypto),
        },
        "order_counts": dict(sorted(Counter(order.status.value for order in orders).items())),
        "fills": [_fill_entry(order, balances) for order in orders if order.status == OrderStatus.CLOSED],
    }


def _dumps(data: dict) -> str:
    """Indented JSON with one line per fill, so a changed fill shows as one changed line in a diff."""
    head = json.dumps({key: value for key, value in data.items() if key != "fills"}, indent=2)
    fills = ",\n".join(f"    {json.dumps(fill)}" for fill in data.get("fills", []))
    return f'{head[:-2]},\n  "fills": [\n{fills}\n  ]\n}}\n'


def save_snapshot(name: str, data: dict, snapshot_dir: Path) -> None:
    """Save snapshot data to a JSON reference file."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / f"{name}.json").write_text(_dumps(data))


def load_snapshot(name: str, snapshot_dir: Path) -> dict | None:
    """Load a snapshot reference file. Returns None if it doesn't exist."""
    path = snapshot_dir / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _same(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    if isinstance(actual, float) or isinstance(expected, float):
        if not (isinstance(actual, int | float) and isinstance(expected, int | float)):
            return False
        if math.isnan(actual) or math.isnan(expected):
            return math.isnan(actual) and math.isnan(expected)
        return math.isclose(actual, expected, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)
    return actual == expected


def _compare_mapping(path: str, actual: dict, expected: dict) -> list[str]:
    diffs = []
    for key in sorted(set(actual) | set(expected)):
        if key not in expected:
            diffs.append(f"  {path}{key}: not in the snapshot, actual={actual[key]!r}")
        elif key not in actual:
            diffs.append(f"  {path}{key}: missing, expected={expected[key]!r}")
        elif not _same(actual[key], expected[key]):
            diffs.append(f"  {path}{key}: expected={expected[key]!r}, actual={actual[key]!r}")
    return diffs


def compare_snapshots(actual: dict, expected: dict) -> list[str]:
    """One line per difference between two snapshots; empty when they match."""
    diffs = []
    for section in sorted(set(actual) ^ set(expected)):
        diffs.append(f"  {section}: section only in the {'result' if section in actual else 'snapshot'}")
    for section in ("metrics", "balances", "order_counts"):
        diffs += _compare_mapping(f"{section}.", actual.get(section, {}), expected.get(section, {}))
    actual_fills = actual.get("fills", [])
    expected_fills = expected.get("fills", [])
    for index, (got, want) in enumerate(zip(actual_fills, expected_fills, strict=False)):
        diffs += _compare_mapping(f"fills[{index}].", got, want)
    for index in range(len(expected_fills), len(actual_fills)):
        diffs.append(f"  fills[{index}]: added {actual_fills[index]!r}")
    for index in range(len(actual_fills), len(expected_fills)):
        diffs.append(f"  fills[{index}]: missing {expected_fills[index]!r}")
    return diffs


def check_snapshot(name: str, actual: dict, snapshot_dir: Path, update: bool) -> None:
    """Compares `actual` with the reference snapshot, or under `update` rewrites it and records what changed."""
    expected = load_snapshot(name, snapshot_dir)
    if update:
        save_snapshot(name, actual, snapshot_dir)
        changes = ["  new snapshot"] if expected is None else compare_snapshots(actual, expected)
        UPDATED_SNAPSHOTS.append((name, changes))
        return
    if expected is None:
        pytest.fail(f"Snapshot {name} missing, run with --update-snapshots")
    diffs = compare_snapshots(actual, expected)
    assert not diffs, f"Snapshot mismatch for {name}:\n" + "\n".join(diffs)
