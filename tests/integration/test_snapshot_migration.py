"""The full-precision snapshots, rounded as the display summary rounds them, equal the 2-decimal ones they replaced.

Delete this file and fixtures/legacy_snapshots/ with the next intentional change that regenerates the snapshots.
"""

from pathlib import Path

import pytest

from .snapshot_utils import load_snapshot

HERE = Path(__file__).resolve().parent
LEGACY_DIR = HERE / "fixtures" / "legacy_snapshots"
SNAPSHOT_DIR = HERE / "snapshots"
NAMES = sorted(path.stem for path in LEGACY_DIR.glob("*.json"))


def _as_displayed(snapshot: dict) -> dict:
    """The fields of the old snapshot format, computed from a new snapshot as the performance summary formats them."""
    metrics = snapshot["metrics"]
    gains = metrics["grid_trading_gains"]
    return {
        "num_buy_trades": metrics["num_buy_trades"],
        "num_sell_trades": metrics["num_sell_trades"],
        "roi": f"{metrics['roi_pct']:.2f}%",
        "total_fees": f"{metrics['total_fees']:.2f}",
        "final_balance_fiat": f"{metrics['final_balance']:.2f}",
        "grid_trading_gains": "N/A" if gains is None else f"{gains:.2f}",
        "max_drawdown": f"{metrics['max_drawdown_pct']:.2f}%",
        "num_orders": len(snapshot["fills"]),
    }


def test_every_snapshot_has_its_legacy_copy():
    assert sorted(path.stem for path in SNAPSHOT_DIR.glob("*.json")) == NAMES
    assert len(NAMES) == 6


@pytest.mark.parametrize("name", NAMES)
def test_the_new_snapshot_rounds_to_the_legacy_one(name):
    assert _as_displayed(load_snapshot(name, SNAPSHOT_DIR)) == load_snapshot(name, LEGACY_DIR)
