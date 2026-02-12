import json
from pathlib import Path
from typing import Any


def extract_snapshot_data(performance_summary: dict, orders: list) -> dict[str, Any]:
    """Extract the deterministic fields from a performance result for snapshot comparison."""
    return {
        "num_buy_trades": performance_summary.get("Number of Buy Trades"),
        "num_sell_trades": performance_summary.get("Number of Sell Trades"),
        "roi": performance_summary.get("ROI"),
        "total_fees": performance_summary.get("Total Fees"),
        "final_balance_fiat": performance_summary.get("Final Balance (Fiat)"),
        "grid_trading_gains": performance_summary.get("Grid Trading Gains"),
        "max_drawdown": performance_summary.get("Max Drawdown"),
        "num_orders": len(orders),
    }


def save_snapshot(name: str, data: dict, snapshot_dir: Path) -> None:
    """Save snapshot data to a JSON reference file."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def load_snapshot(name: str, snapshot_dir: Path) -> dict | None:
    """Load a snapshot reference file. Returns None if it doesn't exist."""
    path = snapshot_dir / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def compare_snapshots(actual: dict, expected: dict, tolerance: float = 1e-6) -> list[str]:
    """Compare two snapshot dicts and return a list of difference descriptions.

    Returns an empty list if snapshots match.
    """
    diffs = []
    all_keys = set(actual.keys()) | set(expected.keys())

    for key in sorted(all_keys):
        actual_val = actual.get(key)
        expected_val = expected.get(key)

        if actual_val == expected_val:
            continue

        # Try numeric comparison with tolerance for string-encoded floats
        try:
            a_num = float(str(actual_val).rstrip("%"))
            e_num = float(str(expected_val).rstrip("%"))
            if abs(a_num - e_num) <= tolerance:
                continue
        except (ValueError, TypeError):
            pass

        diffs.append(f"  {key}: expected={expected_val!r}, actual={actual_val!r}")

    return diffs
