import asyncio
import logging
import threading

import pytest

from grid_trading_bot.utils import logging_config
from grid_trading_bot.utils.logging_config import add_bot_log_file, configure_console, current_bot


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _log_lines(path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


async def _as_bot(name: str, level: int, to_file: bool, messages: list[tuple[int, str]]) -> None:
    current_bot.set(name)
    configure_console(level)
    if to_file:
        add_bot_log_file(name, level)
    logger = logging.getLogger("TestBot")
    for message_level, message in messages:
        logger.log(message_level, message)


async def test_each_bot_file_holds_only_its_own_lines_at_its_own_level(in_tmp, capsys):
    await asyncio.gather(
        _as_bot("bot_a", logging.DEBUG, True, [(logging.DEBUG, "a debug"), (logging.INFO, "a info")]),
        _as_bot("bot_b", logging.INFO, True, [(logging.DEBUG, "b debug"), (logging.INFO, "b info")]),
    )

    a_lines = _log_lines(in_tmp / "logs" / "bot_a.log")
    b_lines = _log_lines(in_tmp / "logs" / "bot_b.log")
    assert any(line.endswith("TestBot - DEBUG - a debug") for line in a_lines)
    assert any(line.endswith("TestBot - INFO - a info") for line in a_lines)
    assert not any("b debug" in line or "b info" in line for line in a_lines)
    assert any(line.endswith("TestBot - INFO - b info") for line in b_lines)
    assert not any("b debug" in line for line in b_lines)
    assert not any("a debug" in line or "a info" in line for line in b_lines)

    console = capsys.readouterr().err.splitlines()
    assert any("[bot_a] TestBot - INFO - a info" in line for line in console)
    assert any("[bot_b] TestBot - INFO - b info" in line for line in console)


async def test_a_file_line_keeps_the_format_alloy_parses(in_tmp):
    await _as_bot("bot_a", logging.INFO, True, [(logging.INFO, "parsed")])

    line = next(line for line in _log_lines(in_tmp / "logs" / "bot_a.log") if "parsed" in line)
    assert "[bot_a]" not in line
    date, rest = line.split(",", 1)
    assert len(date) == len("2026-10-22 12:00:00")
    assert rest[3:] == " - TestBot - INFO - parsed"


async def test_a_record_logged_from_to_thread_reaches_its_bot_file(in_tmp):
    current_bot.set("bot_a")
    configure_console(logging.INFO)
    add_bot_log_file("bot_a", logging.INFO)

    await asyncio.to_thread(logging.getLogger("Worker").info, "from a worker thread")

    assert any("Worker - INFO - from a worker thread" in line for line in _log_lines(in_tmp / "logs" / "bot_a.log"))


async def test_a_record_without_bot_context_goes_to_the_console_only(in_tmp, capsys):
    current_bot.set("bot_a")
    configure_console(logging.INFO)
    add_bot_log_file("bot_a", logging.INFO)

    thread = threading.Thread(target=logging.getLogger("Library").warning, args=("no context",))
    thread.start()
    thread.join()

    assert not any("no context" in line for line in _log_lines(in_tmp / "logs" / "bot_a.log"))
    assert any("[-] Library - WARNING - no context" in line for line in capsys.readouterr().err.splitlines())


async def test_a_record_naming_its_bot_reaches_that_bot_file_whatever_the_context(in_tmp, capsys):
    current_bot.set("bot_b")
    configure_console(logging.INFO)
    add_bot_log_file("bot_a", logging.INFO)
    add_bot_log_file("bot_b", logging.INFO)

    logging.getLogger("Process").error("bot_a failed", extra={"bot": "bot_a"})

    assert any("Process - ERROR - bot_a failed" in line for line in _log_lines(in_tmp / "logs" / "bot_a.log"))
    assert not any("bot_a failed" in line for line in _log_lines(in_tmp / "logs" / "bot_b.log"))
    assert any("[bot_a] Process - ERROR - bot_a failed" in line for line in capsys.readouterr().err.splitlines())


async def test_two_bots_with_the_same_name_share_one_file_without_duplicated_lines(in_tmp):
    await asyncio.gather(
        _as_bot("same", logging.INFO, True, [(logging.INFO, "first bot")]),
        _as_bot("same", logging.INFO, True, [(logging.INFO, "second bot")]),
    )

    lines = _log_lines(in_tmp / "logs" / "same.log")
    assert sum("first bot" in line for line in lines) == 1
    assert sum("second bot" in line for line in lines) == 1
    assert len(logging_config._bot_file_handlers) == 1


async def test_a_bot_without_log_to_file_creates_no_file(in_tmp):
    await _as_bot("bot_c", logging.INFO, False, [(logging.INFO, "console only")])

    assert not (in_tmp / "logs").exists()


async def test_the_console_level_only_goes_down(in_tmp, capsys):
    current_bot.set("bot_a")
    configure_console(logging.INFO)
    configure_console(logging.DEBUG)
    configure_console(logging.WARNING)

    logging.getLogger("Levels").debug("debug after the warning call")

    assert any("debug after the warning call" in line for line in capsys.readouterr().err.splitlines())


async def test_a_file_announces_where_it_is(in_tmp):
    await _as_bot("bot_a", logging.INFO, True, [])

    assert any(
        "File logging enabled. Logs are stored in: logs/bot_a.log" in line
        for line in _log_lines(in_tmp / "logs" / "bot_a.log")
    )


def test_a_log_directory_that_cannot_be_created_raises(in_tmp, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("Directory creation failed")

    monkeypatch.setattr(logging_config.os, "makedirs", refuse)

    with pytest.raises(OSError, match="Directory creation failed"):
        add_bot_log_file("bot_a", logging.INFO)


def test_reset_removes_every_handler_this_module_added(in_tmp):
    before = list(logging.getLogger().handlers)
    configure_console(logging.INFO)
    add_bot_log_file("bot_a", logging.INFO)

    logging_config.reset_logging()

    assert logging.getLogger().handlers == before
