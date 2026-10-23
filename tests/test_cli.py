import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import ccxt
from click.testing import CliRunner
import pandas as pd
import pytest

from grid_trading_bot import cli
from grid_trading_bot.cli import BotOutcome, run_bot
from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.bot_management.process_lifecycle import ProcessLifecycle
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest
from grid_trading_bot.core.persistence.run_files import (
    HaltRecord,
    RunFiles,
    RunLock,
    read_halt_marker,
    write_halt_marker,
)


@pytest.fixture(autouse=True)
def no_real_notifications(monkeypatch):
    monkeypatch.setenv("APPRISE_NOTIFICATION_URLS", "")
    monkeypatch.setenv("EXCHANGE_API_KEY", "test")
    monkeypatch.setenv("EXCHANGE_SECRET_KEY", "test")


def _write_config(tmp_path, valid_config, mode="live"):
    config = json.loads(json.dumps(valid_config))
    config["exchange"]["trading_mode"] = mode
    config["persistence"] = {"enabled": True, "db_path": str(tmp_path / "data" / "state.db")}
    config["logging"] = {"log_level": "INFO", "log_to_file": False}
    path = tmp_path / f"config_{mode}.json"
    path.write_text(json.dumps(config))
    return str(path)


def _config_manager(path):
    return ConfigManager(path)


def _fake_bot(result=None, stop_request=None, run_side_effect=None):
    bot = MagicMock()
    bot.run = AsyncMock(return_value=result, side_effect=run_side_effect)
    bot.stop_request = stop_request
    return bot


@pytest.fixture
def patched_components():
    with (
        patch("grid_trading_bot.cli.GridTradingBot") as bot_cls,
        patch("grid_trading_bot.cli.HealthCheck") as health_cls,
        patch("grid_trading_bot.cli.BotController") as controller_cls,
    ):
        health_cls.return_value.start = AsyncMock()
        controller_cls.return_value.command_listener = AsyncMock()
        yield bot_cls, health_cls, controller_cls


@pytest.mark.asyncio
async def test_a_halted_config_refuses_to_build_the_bot(tmp_path, valid_config, patched_components):
    bot_cls, *_ = patched_components
    path = _write_config(tmp_path, valid_config)
    config_manager = _config_manager(path)
    write_halt_marker(
        RunFiles.for_config(config_manager).halt_path, HaltRecord("risk", "TP or SL hit.", "ETH/USDT", "t")
    )

    outcome = await run_bot(path, config_manager, "bot_test", ProcessLifecycle())

    assert outcome == BotOutcome(0)
    bot_cls.assert_not_called()


@pytest.mark.asyncio
async def test_the_own_halt_marker_recording_the_config_hash_refuses_to_build_the_bot(
    tmp_path, valid_config, patched_components
):
    bot_cls, *_ = patched_components
    path = _write_config(tmp_path, valid_config)
    config_manager = _config_manager(path)
    record = HaltRecord("risk", "TP or SL hit.", "ETH/USDT", "t", config_hash=config_manager.get_config_hash())
    write_halt_marker(RunFiles.for_config(config_manager).halt_path, record)

    outcome = await run_bot(path, config_manager, "bot_test", ProcessLifecycle())

    assert outcome == BotOutcome(0)
    bot_cls.assert_not_called()


def _marker_beside(config_manager, name, config_hash=None):
    path = os.path.join(os.path.dirname(RunFiles.for_config(config_manager).halt_path), name)
    write_halt_marker(path, HaltRecord("risk", "TP or SL hit.", "ETH/USDT", "t", config_hash=config_hash))
    return path


@pytest.mark.asyncio
async def test_a_marker_beside_without_a_recorded_hash_refuses_to_build_the_bot(
    tmp_path, valid_config, patched_components, caplog
):
    bot_cls, *_ = patched_components
    path = _write_config(tmp_path, valid_config)
    config_manager = _config_manager(path)
    legacy = _marker_beside(config_manager, "halted_0badc0de.json")

    with patch.object(cli.NotificationHandler, "async_send_notification", new_callable=AsyncMock) as notify:
        outcome = await run_bot(path, config_manager, "bot_test", ProcessLifecycle())

    assert outcome == BotOutcome(0)
    bot_cls.assert_not_called()
    assert legacy in caplog.text
    assert notify.await_args.args[0] is cli.NotificationType.START_REFUSED
    assert legacy in notify.await_args.kwargs["reason"]


@pytest.mark.asyncio
async def test_a_marker_beside_recording_another_hash_is_ignored(tmp_path, valid_config, patched_components):
    bot_cls, *_ = patched_components
    path = _write_config(tmp_path, valid_config)
    config_manager = _config_manager(path)
    _marker_beside(config_manager, "halted_0badc0de.json", config_hash="0badc0de" + "0" * 56)
    bot_cls.return_value = _fake_bot(stop_request=StopRequest(StopKind.MANUAL, "stop"))

    outcome = await run_bot(path, config_manager, "bot_test", ProcessLifecycle())

    assert outcome == BotOutcome(0)
    bot_cls.assert_called_once()


@pytest.mark.asyncio
async def test_a_locked_config_refuses_to_build_the_bot(tmp_path, valid_config, patched_components):
    bot_cls, *_ = patched_components
    path = _write_config(tmp_path, valid_config)
    config_manager = _config_manager(path)
    holder = RunLock(RunFiles.for_config(config_manager).lock_path)
    assert holder.acquire()
    try:
        outcome = await run_bot(path, config_manager, "bot_test", ProcessLifecycle())
    finally:
        holder.release()

    assert outcome.exit_code == 1
    bot_cls.assert_not_called()


@pytest.mark.asyncio
async def test_the_stop_request_sets_the_exit_code(tmp_path, valid_config, patched_components):
    bot_cls, *_ = patched_components
    path = _write_config(tmp_path, valid_config)
    request = StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15)
    bot_cls.return_value = _fake_bot(result={"config": path}, stop_request=request)

    outcome = await run_bot(path, _config_manager(path), "bot_test", ProcessLifecycle())

    assert outcome == BotOutcome(143, {"config": path})


@pytest.mark.asyncio
async def test_a_crash_without_a_stop_request_exits_1(tmp_path, valid_config, patched_components):
    bot_cls, *_ = patched_components
    path = _write_config(tmp_path, valid_config)
    bot_cls.return_value = _fake_bot(run_side_effect=RuntimeError("boom"))

    outcome = await run_bot(path, _config_manager(path), "bot_test", ProcessLifecycle())

    assert outcome.exit_code == 1


@pytest.mark.asyncio
async def test_a_backtest_takes_no_lock_and_exits_0(tmp_path, valid_config, patched_components):
    bot_cls, *_ = patched_components
    path = _write_config(tmp_path, valid_config, mode="backtest")
    bot_cls.return_value = _fake_bot(result={"config": path})

    outcome = await run_bot(path, _config_manager(path), "bot_test", ProcessLifecycle())

    assert outcome == BotOutcome(0, {"config": path})
    assert not (tmp_path / "data").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(("is_tty", "listening"), [(True, True), (False, False)])
async def test_the_listener_runs_only_with_a_terminal(tmp_path, valid_config, patched_components, is_tty, listening):
    bot_cls, _, controller_cls = patched_components
    path = _write_config(tmp_path, valid_config)
    bot_cls.return_value = _fake_bot(stop_request=StopRequest(StopKind.MANUAL, "stop"))

    with patch("grid_trading_bot.cli.sys.stdin") as stdin:
        stdin.isatty.return_value = is_tty
        await run_bot(path, _config_manager(path), "bot_test", ProcessLifecycle())

    assert controller_cls.return_value.command_listener.called is listening


def test_a_config_error_exits_78_and_starts_nothing(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"exchange": {}}))

    with patch("grid_trading_bot.cli.run_bot") as mock_run_bot:
        result = CliRunner().invoke(cli.main, ["run", "--config", str(bad)])

    assert result.exit_code == 78
    mock_run_bot.assert_not_called()


def test_run_exits_with_the_most_severe_code_and_saves_results(tmp_path, valid_config):
    first = _write_config(tmp_path, valid_config)
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _write_config(second_dir, valid_config, mode="backtest")
    results_path = tmp_path / "results.json"
    outcomes = {first: BotOutcome(143, None), second: BotOutcome(0, {"config": second})}

    async def fake_run_bot(path, *_args, **_kwargs):
        return outcomes[path]

    with (
        patch("grid_trading_bot.cli.run_bot", side_effect=fake_run_bot),
        patch("grid_trading_bot.cli.save_or_append_performance_results") as mock_save,
    ):
        result = CliRunner().invoke(
            cli.main, ["run", "--config", first, "--config", second, "--save_performance_results", str(results_path)]
        )

    assert result.exit_code == 143
    mock_save.assert_called_once_with({"config": second}, str(results_path))


def test_a_backtest_whose_candles_failed_to_load_exits_1_and_saves_no_results(tmp_path, valid_config):
    config = json.loads(json.dumps(valid_config))
    config["trading_settings"]["historical_data_file"] = str(tmp_path / "missing.csv")
    config["logging"] = {"log_level": "INFO", "log_to_file": False}
    path = tmp_path / "config_backtest.json"
    path.write_text(json.dumps(config))
    results_path = tmp_path / "results.json"

    result = CliRunner().invoke(
        cli.main, ["run", "--config", str(path), "--no-plot", "--save_performance_results", str(results_path)]
    )

    assert result.exit_code == 1
    assert not results_path.exists()


def test_a_backtest_whose_dates_hold_no_candle_exits_1_and_saves_no_results(tmp_path, valid_config):
    csv = tmp_path / "candles.csv"
    csv.write_text(
        "timestamp,open,high,low,close,volume\n"
        "2024-08-01 00:00:00,171.7,172.15,171.57,171.81,8036.24\n"
        "2024-08-01 00:01:00,171.8,171.97,171.74,171.86,3712.19\n"
    )
    config = json.loads(json.dumps(valid_config))
    config["trading_settings"]["historical_data_file"] = str(csv)
    config["trading_settings"]["period"] = {"start_date": "2025-01-01T00:00:00Z", "end_date": "2025-01-02T00:00:00Z"}
    config["logging"] = {"log_level": "INFO", "log_to_file": False}
    path = tmp_path / "config_backtest.json"
    path.write_text(json.dumps(config))
    results_path = tmp_path / "results.json"

    result = CliRunner().invoke(
        cli.main, ["run", "--config", str(path), "--no-plot", "--save_performance_results", str(results_path)]
    )

    assert result.exit_code == 1
    assert not results_path.exists()


def test_an_empty_ccxt_range_exits_1_through_the_cli(tmp_path, valid_config, monkeypatch, caplog):
    class EmptyExchange:
        def __init__(self):
            self.timeframes = {"1h": "1h"}

        @staticmethod
        def parse8601(date):
            return int(pd.Timestamp(date).timestamp() * 1000)

        def load_markets(self):
            return {"ETH/USDT": {}}

        def fetch_ohlcv(self, pair, timeframe, since=None, limit=None):
            return []

    monkeypatch.setattr(ccxt, "binance", EmptyExchange)
    config = json.loads(json.dumps(valid_config))
    config["trading_settings"]["historical_data_file"] = None
    config["trading_settings"]["timeframe"] = "1h"
    config["trading_settings"]["period"] = {"start_date": "2024-01-01T00:00:00Z", "end_date": "2024-01-02T00:00:00Z"}
    config["logging"] = {"log_level": "INFO", "log_to_file": False}
    path = tmp_path / "config_backtest.json"
    path.write_text(json.dumps(config))
    results_path = tmp_path / "results.json"

    result = CliRunner().invoke(
        cli.main, ["run", "--config", str(path), "--no-plot", "--save_performance_results", str(results_path)]
    )

    assert result.exit_code == 1
    assert not results_path.exists()
    assert "No candles for ETH/USDT between 2024-01-01T00:00:00Z and 2024-01-02T00:00:00Z" in caplog.text


def test_the_traceback_of_a_failed_bot_reaches_that_bot_log_file_only(tmp_path, valid_config, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = []
    for num_grids in (10, 20):
        config = json.loads(json.dumps(valid_config))
        config["grid_strategy"]["num_grids"] = num_grids
        config["logging"] = {"log_level": "INFO", "log_to_file": True}
        path = tmp_path / f"config_{num_grids}.json"
        path.write_text(json.dumps(config))
        paths.append(str(path))

    async def fake_run_bot(config_path, *_args, **_kwargs):
        if config_path == paths[0]:
            raise RuntimeError("bot 10 exploded")
        return BotOutcome(0)

    with (
        patch(
            "grid_trading_bot.cli.generate_config_name",
            side_effect=lambda cm: f"bot_{cm.config.grid_strategy.num_grids}",
        ),
        patch("grid_trading_bot.cli._run_bot", side_effect=fake_run_bot),
    ):
        result = CliRunner().invoke(cli.main, ["run", "--config", paths[0], "--config", paths[1]])

    assert result.exit_code == 1
    failed = (tmp_path / "logs" / "bot_10.log").read_text()
    assert f"Error occurred while running bot for config {paths[0]}: bot 10 exploded" in failed
    assert "RuntimeError: bot 10 exploded" in failed
    assert "bot 10 exploded" not in (tmp_path / "logs" / "bot_20.log").read_text()


def test_resume_archives_the_state_and_clears_the_halt(tmp_path, valid_config):
    path = _write_config(tmp_path, valid_config)
    files = RunFiles.for_config(_config_manager(path))
    os.makedirs(os.path.dirname(files.db_path), exist_ok=True)
    with open(files.db_path, "w") as db:
        db.write("x")
    write_halt_marker(files.halt_path, HaltRecord("manual", "stop", "ETH/USDT", "t"))

    result = CliRunner().invoke(cli.main, ["resume", "--config", path])

    assert result.exit_code == 0
    assert not os.path.exists(files.halt_path)
    assert not os.path.exists(files.db_path)
    prefix = os.path.basename(files.db_path) + ".halted-"
    assert any(name.startswith(prefix) for name in os.listdir(os.path.dirname(files.db_path)))


def test_resume_without_a_halt_changes_nothing(tmp_path, valid_config):
    path = _write_config(tmp_path, valid_config)
    files = RunFiles.for_config(_config_manager(path))
    os.makedirs(os.path.dirname(files.db_path), exist_ok=True)
    with open(files.db_path, "w") as db:
        db.write("x")

    result = CliRunner().invoke(cli.main, ["resume", "--config", path])

    assert result.exit_code == 0
    assert os.path.exists(files.db_path)
    assert "No halt recorded" in result.output


def test_resume_refuses_while_a_bot_holds_the_lock(tmp_path, valid_config):
    path = _write_config(tmp_path, valid_config)
    files = RunFiles.for_config(_config_manager(path))
    write_halt_marker(files.halt_path, HaltRecord("manual", "stop", "ETH/USDT", "t"))
    holder = RunLock(files.lock_path)
    assert holder.acquire()
    try:
        result = CliRunner().invoke(cli.main, ["resume", "--config", path])
    finally:
        holder.release()

    assert result.exit_code == 1
    assert os.path.exists(files.halt_path)


def test_resume_clears_the_markers_beside_without_a_recorded_hash(tmp_path, valid_config):
    path = _write_config(tmp_path, valid_config)
    config_manager = _config_manager(path)
    files = RunFiles.for_config(config_manager)
    os.makedirs(os.path.dirname(files.db_path), exist_ok=True)
    with open(files.db_path, "w") as db:
        db.write("x")
    legacy = _marker_beside(config_manager, "halted_0badc0de.json")
    other_config = _marker_beside(config_manager, "halted_feedf00d.json", config_hash="feedf00d" + "0" * 56)

    result = CliRunner().invoke(cli.main, ["resume", "--config", path])

    assert result.exit_code == 0
    assert legacy in result.output
    assert not os.path.exists(legacy)
    assert read_halt_marker(other_config) is not None
    assert os.path.exists(files.db_path)


def test_resume_clears_the_own_halt_and_the_markers_beside_without_a_hash(tmp_path, valid_config):
    path = _write_config(tmp_path, valid_config)
    config_manager = _config_manager(path)
    files = RunFiles.for_config(config_manager)
    write_halt_marker(files.halt_path, HaltRecord("manual", "stop", "ETH/USDT", "t", config_manager.get_config_hash()))
    legacy = _marker_beside(config_manager, "halted_0badc0de.json")

    result = CliRunner().invoke(cli.main, ["resume", "--config", path])

    assert result.exit_code == 0
    assert not os.path.exists(files.halt_path)
    assert not os.path.exists(legacy)


@pytest.mark.parametrize("mode", ["live", "paper_trading"])
@pytest.mark.parametrize("urls", ["", " , "])
def test_live_without_urls_warns_once(tmp_path, valid_config, caplog, monkeypatch, mode, urls):
    monkeypatch.setenv("APPRISE_NOTIFICATION_URLS", urls)
    config_manager = _config_manager(_write_config(tmp_path, valid_config, mode))

    handler = cli.initialize_notification_handler(config_manager)

    warnings = [r for r in caplog.records if "Notifications are disabled" in r.getMessage()]
    assert len(warnings) == 1
    assert "APPRISE_NOTIFICATION_URLS is empty." in warnings[0].getMessage()
    assert handler.enabled is False


def test_a_backtest_without_urls_does_not_warn(tmp_path, valid_config, caplog):
    config_manager = _config_manager(_write_config(tmp_path, valid_config, "backtest"))

    handler = cli.initialize_notification_handler(config_manager)

    assert "Notifications are disabled" not in caplog.text
    assert handler.enabled is False


@pytest.mark.parametrize(("mode", "enabled"), [("live", True), ("paper_trading", True), ("backtest", False)])
def test_only_live_and_paper_send_alerts(tmp_path, valid_config, monkeypatch, mode, enabled):
    monkeypatch.setenv("APPRISE_NOTIFICATION_URLS", "json://localhost")
    config_manager = _config_manager(_write_config(tmp_path, valid_config, mode))

    assert cli.initialize_notification_handler(config_manager).enabled is enabled
