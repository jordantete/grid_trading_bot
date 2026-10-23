import ast
from pathlib import Path

import grid_trading_bot

PACKAGE_ROOT = Path(grid_trading_bot.__file__).parent

MODE_READERS = {
    "cli.py",
    "core/bot_management/grid_trading_bot.py",
    "core/bot_management/bot_assembly.py",
    "core/services/exchange_service_factory.py",
    "core/order_handling/execution_strategy/order_execution_strategy_factory.py",
}


def _names_trading_mode(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "TradingMode":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "TradingMode":
            return True
        if isinstance(node, ast.alias) and "TradingMode" in (node.name.rsplit(".", 1)[-1], node.asname):
            return True
    return False


def test_trading_mode_is_read_only_where_the_bot_is_assembled():
    """
    Only the config, the CLI, the bot assembly and the two factories name `TradingMode`; every other component
    receives the behaviour its mode needs. The scan catches the name only: a branch on `.trading_mode.value`
    compared to a string would slip through.
    """
    readers = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if _names_trading_mode(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            readers.add(relative)

    assert "cli.py" in readers
    assert {reader for reader in readers if not reader.startswith("config/")} <= MODE_READERS
