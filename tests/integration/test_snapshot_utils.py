import json

import pytest

from . import snapshot_utils
from .snapshot_utils import check_snapshot, compare_snapshots, load_snapshot, save_snapshot


def _fill(**overrides):
    fill = {
        "identifier": "backtest-1",
        "side": "buy",
        "order_type": "limit",
        "price": 155.0,
        "average": 155.0,
        "filled": 0.11041022473498231,
        "fee_cost": 0.00011041022473498231,
        "fee_currency": "SOL",
        "timestamp": 1722611640,
    }
    return {**fill, **overrides}


def _snapshot(**sections):
    snapshot = {
        "metrics": {"roi_pct": 0.07, "num_buy_trades": 10, "armed": True, "stop_reason": "stop_loss"},
        "balances": {"fiat": "150.10066330", "crypto": "0E-8", "reserved_fiat": "0E-8", "reserved_crypto": "0E-8"},
        "order_counts": {"closed": 1},
        "fills": [_fill()],
    }
    return {**snapshot, **sections}


class TestCompareSnapshots:
    def test_identical_snapshots_match(self):
        assert compare_snapshots(_snapshot(), _snapshot()) == []

    def test_a_float_within_the_relative_tolerance_matches(self):
        actual = _snapshot(metrics={**_snapshot()["metrics"], "roi_pct": 0.07 * (1 + 1e-12)})

        assert compare_snapshots(actual, _snapshot()) == []

    def test_a_float_outside_the_relative_tolerance_differs(self):
        actual = _snapshot(metrics={**_snapshot()["metrics"], "roi_pct": 0.07 * (1 + 1e-6)})

        assert compare_snapshots(actual, _snapshot()) == [
            f"  metrics.roi_pct: expected=0.07, actual={0.07 * (1 + 1e-6)!r}"
        ]

    def test_two_nans_match(self):
        nan_metrics = {**_snapshot()["metrics"], "roi_pct": float("nan")}

        assert compare_snapshots(_snapshot(metrics=nan_metrics), _snapshot(metrics=nan_metrics)) == []

    def test_a_decimal_string_compares_exactly(self):
        actual = _snapshot(balances={**_snapshot()["balances"], "fiat": "150.10066331"})

        assert compare_snapshots(actual, _snapshot()) == [
            "  balances.fiat: expected='150.10066330', actual='150.10066331'"
        ]

    def test_a_boolean_never_matches_a_number(self):
        actual = _snapshot(metrics={**_snapshot()["metrics"], "armed": 1})

        assert compare_snapshots(actual, _snapshot()) == ["  metrics.armed: expected=True, actual=1"]

    def test_a_missing_key_and_an_extra_key_are_both_reported(self):
        metrics = dict(_snapshot()["metrics"])
        del metrics["stop_reason"]
        metrics["new_metric"] = 1.5

        assert compare_snapshots(_snapshot(metrics=metrics), _snapshot()) == [
            "  metrics.new_metric: not in the snapshot, actual=1.5",
            "  metrics.stop_reason: missing, expected='stop_loss'",
        ]

    def test_a_changed_fill_is_reported_by_index_and_field(self):
        actual = _snapshot(fills=[_fill(price=156.0)])

        assert compare_snapshots(actual, _snapshot()) == ["  fills[0].price: expected=155.0, actual=156.0"]

    def test_an_added_fill_is_reported(self):
        extra = _fill(identifier="backtest-2", side="sell")
        actual = _snapshot(fills=[_fill(), extra])

        assert compare_snapshots(actual, _snapshot()) == [f"  fills[1]: added {extra!r}"]

    def test_a_missing_fill_is_reported(self):
        assert compare_snapshots(_snapshot(fills=[]), _snapshot()) == [f"  fills[0]: missing {_fill()!r}"]

    def test_a_section_missing_from_the_snapshot_is_reported(self):
        expected = _snapshot()
        del expected["order_counts"]

        assert compare_snapshots(_snapshot(), expected) == [
            "  order_counts: section only in the result",
            "  order_counts.closed: not in the snapshot, actual=1",
        ]


class TestSnapshotFiles:
    def test_a_saved_snapshot_loads_back_unchanged(self, tmp_path):
        save_snapshot("scenario", _snapshot(), tmp_path)

        assert load_snapshot("scenario", tmp_path) == _snapshot()
        assert json.loads((tmp_path / "scenario.json").read_text()) == _snapshot()

    def test_each_fill_takes_one_line(self, tmp_path):
        save_snapshot("scenario", _snapshot(fills=[_fill(), _fill(identifier="backtest-2")]), tmp_path)

        lines = (tmp_path / "scenario.json").read_text().splitlines()
        assert sum(line.strip().startswith('{"identifier"') for line in lines) == 2

    def test_a_snapshot_without_fills_is_valid_json(self, tmp_path):
        save_snapshot("scenario", _snapshot(fills=[]), tmp_path)

        assert load_snapshot("scenario", tmp_path)["fills"] == []

    def test_a_missing_snapshot_fails_and_writes_nothing(self, tmp_path):
        with pytest.raises(pytest.fail.Exception, match="Snapshot scenario missing, run with --update-snapshots"):
            check_snapshot("scenario", _snapshot(), tmp_path, update=False)

        assert not (tmp_path / "scenario.json").exists()

    def test_an_update_rewrites_the_snapshot_and_records_its_changes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(snapshot_utils, "UPDATED_SNAPSHOTS", [])
        save_snapshot("scenario", _snapshot(), tmp_path)
        changed = _snapshot(fills=[_fill(price=156.0)])

        check_snapshot("scenario", changed, tmp_path, update=True)

        assert load_snapshot("scenario", tmp_path) == changed
        assert snapshot_utils.UPDATED_SNAPSHOTS == [("scenario", ["  fills[0].price: expected=155.0, actual=156.0"])]

    def test_an_update_of_a_new_snapshot_records_it_as_new(self, tmp_path, monkeypatch):
        monkeypatch.setattr(snapshot_utils, "UPDATED_SNAPSHOTS", [])

        check_snapshot("scenario", _snapshot(), tmp_path, update=True)

        assert snapshot_utils.UPDATED_SNAPSHOTS == [("scenario", ["  new snapshot"])]

    def test_a_mismatch_fails_with_every_difference(self, tmp_path):
        save_snapshot("scenario", _snapshot(), tmp_path)

        with pytest.raises(AssertionError, match=r"Snapshot mismatch for scenario:\n  fills\[0\]\.price"):
            check_snapshot("scenario", _snapshot(fills=[_fill(price=156.0)]), tmp_path, update=False)
