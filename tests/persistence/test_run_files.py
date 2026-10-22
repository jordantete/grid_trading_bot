from datetime import UTC, datetime
import json
import os
from unittest.mock import Mock

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.persistence.run_files import (
    HaltRecord,
    RunFiles,
    RunLock,
    archive_state_db,
    legacy_halt_markers,
    read_halt_marker,
    remove_halt_marker,
    write_halt_marker,
)


def test_run_files_sit_next_to_the_state_db_and_carry_the_config_hash(tmp_path):
    config_manager = Mock(spec=ConfigManager)
    config_manager.get_state_db_path.return_value = str(tmp_path / "data" / "BTC_USDT" / "state_abcd1234.db")
    config_manager.get_config_hash.return_value = "abcd1234ffff"

    files = RunFiles.for_config(config_manager)

    assert files.db_path == str(tmp_path / "data" / "BTC_USDT" / "state_abcd1234.db")
    assert files.lock_path == str(tmp_path / "data" / "BTC_USDT" / "bot_abcd1234.lock")
    assert files.halt_path == str(tmp_path / "data" / "BTC_USDT" / "halted_abcd1234.json")


def test_halt_marker_round_trip(tmp_path):
    path = str(tmp_path / "nested" / "halted_abcd1234.json")
    record = HaltRecord(kind="risk", reason="TP or SL hit.", pair="BTC/USDT", halted_at="2026-10-16T09:00:00+00:00")

    write_halt_marker(path, record)

    assert read_halt_marker(path) == record
    assert not os.path.exists(f"{path}.tmp")


def test_a_marker_records_the_config_hash(tmp_path):
    path = str(tmp_path / "halted_abcd1234.json")
    record = HaltRecord("manual", "stop", "BTC/USDT", "t", config_hash="abcd1234" + "0" * 56)

    write_halt_marker(path, record)

    assert read_halt_marker(path) == record


def test_a_marker_written_without_a_hash_reads_with_none(tmp_path):
    path = tmp_path / "halted_abcd1234.json"
    path.write_text(json.dumps({"kind": "manual", "reason": "r", "pair": "p", "halted_at": "t"}))

    assert read_halt_marker(str(path)).config_hash is None


def test_legacy_markers_are_the_markers_beside_that_record_no_hash(tmp_path):
    own = tmp_path / "halted_aaaaaaaa.json"
    legacy = tmp_path / "halted_bbbbbbbb.json"
    unreadable = tmp_path / "halted_cccccccc.json"
    other_config = tmp_path / "halted_dddddddd.json"
    write_halt_marker(str(own), HaltRecord("manual", "stop", "BTC/USDT", "t"))
    write_halt_marker(str(legacy), HaltRecord("risk", "TP hit.", "BTC/USDT", "t"))
    unreadable.write_text('{"kind": "risk", "reas')
    write_halt_marker(str(other_config), HaltRecord("manual", "stop", "BTC/USDT", "t", config_hash="d" * 64))
    (tmp_path / "state_bbbbbbbb.db").write_text("x")

    assert legacy_halt_markers(str(own)) == [str(legacy), str(unreadable)]


def test_no_legacy_marker_without_the_directory(tmp_path):
    assert legacy_halt_markers(str(tmp_path / "absent" / "halted_x.json")) == []


def test_no_marker_reads_as_none(tmp_path):
    assert read_halt_marker(str(tmp_path / "halted_x.json")) is None


def test_unreadable_marker_still_counts_as_halted(tmp_path):
    path = tmp_path / "halted_x.json"
    path.write_text('{"kind": "risk", "reas')

    record = read_halt_marker(str(path))

    assert record is not None
    assert record.kind == "unknown"
    assert str(path) in record.reason


def test_remove_halt_marker_ignores_a_missing_file(tmp_path):
    path = tmp_path / "halted_x.json"
    path.write_text(json.dumps({"kind": "manual", "reason": "r", "pair": "p", "halted_at": "t"}))

    remove_halt_marker(str(path))
    remove_halt_marker(str(path))

    assert not path.exists()


def test_run_lock_refuses_a_second_holder(tmp_path):
    path = str(tmp_path / "missing_dir" / "bot_x.lock")
    first, second = RunLock(path), RunLock(path)

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_archive_moves_the_db_and_its_side_files(tmp_path):
    db = tmp_path / "state_abcd1234.db"
    for path in (db, tmp_path / "state_abcd1234.db-wal", tmp_path / "state_abcd1234.db-shm"):
        path.write_text("x")

    moved = archive_state_db(str(db), datetime(2026, 10, 16, 9, 30, 5, tzinfo=UTC))

    assert sorted(os.path.basename(p) for p in moved) == [
        "state_abcd1234.db-shm.halted-20261016T093005Z",
        "state_abcd1234.db-wal.halted-20261016T093005Z",
        "state_abcd1234.db.halted-20261016T093005Z",
    ]
    assert not db.exists()


def test_archive_without_a_db_moves_nothing(tmp_path):
    assert archive_state_db(str(tmp_path / "state_x.db"), datetime(2026, 10, 16, tzinfo=UTC)) == []
