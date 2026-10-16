import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner
import pytest

from grid_trading_bot import cli
from grid_trading_bot.cli import BotOutcome, run_bot
from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.config_validator import ConfigValidator
from grid_trading_bot.core.bot_management.process_lifecycle import ProcessLifecycle
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest
from grid_trading_bot.core.persistence.run_files import HaltRecord, RunFiles, RunLock, write_halt_marker


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
    return ConfigManager(path, ConfigValidator())


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

    outcome = await run_bot(path, config_manager, ProcessLifecycle())

    assert outcome == BotOutcome(0)
    bot_cls.assert_not_called()


@pytest.mark.asyncio
async def test_a_locked_config_refuses_to_build_the_bot(tmp_path, valid_config, patched_components):
    bot_cls, *_ = patched_components
    path = _write_config(tmp_path, valid_config)
    config_manager = _config_manager(path)
    holder = RunLock(RunFiles.for_config(config_manager).lock_path)
    assert holder.acquire()
    try:
        outcome = await run_bot(path, config_manager, ProcessLifecycle())
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

    outcome = await run_bot(path, _config_manager(path), ProcessLifecycle())

    assert outcome == BotOutcome(143, {"config": path})


@pytest.mark.asyncio
async def test_a_crash_without_a_stop_request_exits_1(tmp_path, valid_config, patched_components):
    bot_cls, *_ = patched_components
    path = _write_config(tmp_path, valid_config)
    bot_cls.return_value = _fake_bot(run_side_effect=RuntimeError("boom"))

    outcome = await run_bot(path, _config_manager(path), ProcessLifecycle())

    assert outcome.exit_code == 1


@pytest.mark.asyncio
async def test_a_backtest_takes_no_lock_and_exits_0(tmp_path, valid_config, patched_components):
    bot_cls, *_ = patched_components
    path = _write_config(tmp_path, valid_config, mode="backtest")
    bot_cls.return_value = _fake_bot(result={"config": path})

    outcome = await run_bot(path, _config_manager(path), ProcessLifecycle())

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
        await run_bot(path, _config_manager(path), ProcessLifecycle())

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
