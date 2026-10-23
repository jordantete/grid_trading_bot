"""Every log text the Grafana dashboard filters on must still exist in the bot's source code."""

import ast
import json
from pathlib import Path
import re

import pytest

from grid_trading_bot.core.bot_management.health_check import format_account_metrics
from grid_trading_bot.core.order_handling.balance_tracker import AccountSnapshot

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = REPO_ROOT / "monitoring" / "dashboards" / "grid_trading_bot_dashboard.json"
SOURCE_DIR = REPO_ROOT / "src" / "grid_trading_bot"

LINE_FILTER = re.compile(r'\|= "((?:[^"\\]|\\.)*)"')
REGEX_FILTER = re.compile(r'\|~ "((?:[^"\\]|\\.)*)"')
REGEXP_STAGE = re.compile(r'\| regexp "((?:[^"\\]|\\.)*)"')
SERVICE_LABEL = re.compile(r'service="([^"]*)"')
LOG_METHODS = {"debug", "info", "warning", "error", "critical", "exception"}


def _source_texts() -> tuple[list[str], set[str]]:
    fragments: list[str] = []
    class_names: set[str] = set()
    for path in SOURCE_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                fragments.append(node.value)
            elif isinstance(node, ast.ClassDef):
                class_names.add(node.name)
    return fragments, class_names


def _is_log_call(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in LOG_METHODS):
        return False
    owner = node.func.value
    return (isinstance(owner, ast.Attribute) and owner.attr == "logger") or (
        isinstance(owner, ast.Name) and owner.id == "logger"
    )


def _strings_under(nodes: list[ast.AST]) -> list[str]:
    return [
        n.value for root in nodes for n in ast.walk(root) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _emitted_texts_by_class() -> dict[str, list[str]]:
    """The text a logger class can put in a log line: its file's log calls and its module-level formatter functions."""
    emitted: dict[str, list[str]] = {}
    for path in SOURCE_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text())
        texts = _strings_under([n for n in ast.walk(tree) if _is_log_call(n)])
        texts += _strings_under([n for n in tree.body if isinstance(n, ast.FunctionDef)])
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                emitted.setdefault(node.name, []).extend(texts)
    return emitted


def _logger_filters(expr: str) -> tuple[list[str], list[str]]:
    """The logger classes a query names (service label or quoted class name) and the message texts it filters on."""
    texts = LINE_FILTER.findall(expr)
    for alternatives in REGEX_FILTER.findall(expr):
        texts.extend(alternatives.split("|"))
    loggers = SERVICE_LABEL.findall(expr) + [text for text in texts if text in CLASS_NAMES]
    return loggers, [text for text in texts if text not in CLASS_NAMES]


def _queries() -> list[tuple[str, str]]:
    dashboard = json.loads(DASHBOARD.read_text())
    return [(panel["title"], target["expr"]) for panel in dashboard["panels"] for target in panel.get("targets", [])]


def _required_texts(expr: str) -> list[str]:
    texts = LINE_FILTER.findall(expr)
    for alternatives in REGEX_FILTER.findall(expr):
        texts.extend(alternatives.split("|"))
    for pattern in REGEXP_STAGE.findall(expr):
        # A regexp field prefix like "'cpu': " comes from a dict repr at runtime, so only its words are checked.
        prefix = pattern.split("(", 1)[0].strip(" '\":")
        if prefix:
            texts.append(prefix)
    return texts


FRAGMENTS, CLASS_NAMES = _source_texts()
EMITTED_BY_CLASS = _emitted_texts_by_class()
QUERIES = _queries()


@pytest.mark.parametrize(("panel", "expr"), QUERIES, ids=[f"{panel}-{i}" for i, (panel, _) in enumerate(QUERIES)])
def test_dashboard_filters_match_source_text(panel, expr):
    missing = [
        text
        for text in _required_texts(expr)
        if text not in CLASS_NAMES and not any(text in fragment for fragment in FRAGMENTS)
    ]
    assert not missing, f"Panel {panel!r} filters on text that no longer exists in src/: {missing}"


@pytest.mark.parametrize(("panel", "expr"), QUERIES, ids=[f"{panel}-{i}" for i, (panel, _) in enumerate(QUERIES)])
def test_dashboard_messages_come_from_the_logger_the_query_names(panel, expr):
    """A filter like "Buy" is a runtime value, so a named logger must emit at least one of the query's texts."""
    loggers, messages = _logger_filters(expr)
    orphans = [
        logger
        for logger in loggers
        if not any(message in text for message in messages for text in EMITTED_BY_CLASS.get(logger, []))
    ]
    assert not orphans, f"Panel {panel!r} names loggers that log none of {messages}: {orphans}"


def test_dashboard_service_labels_are_class_names():
    unknown = [
        (panel, service)
        for panel, expr in QUERIES
        for service in SERVICE_LABEL.findall(expr)
        if service not in CLASS_NAMES
    ]
    assert not unknown, f"service labels without a matching logger class: {unknown}"


def test_account_metric_regexes_capture_the_logged_values_with_their_sign():
    line = format_account_metrics(
        AccountSnapshot(
            price=0.00001234, value=1234.5, peak=1300.0, drawdown_pct=5.04, realized_pnl=-3.25, unrealized_pnl=-78.9
        )
    )
    captured: dict[str, str] = {}
    for _, expr in QUERIES:
        if '"Account metrics"' not in expr:
            continue
        for pattern in REGEXP_STAGE.findall(expr):
            match = re.search(pattern.replace("\\\\", "\\"), line)
            assert match, f"{pattern!r} does not match {line!r}"
            captured.update(match.groupdict())

    assert {name: float(value) for name, value in captured.items()} == {
        "value": 1234.5,
        "peak": 1300.0,
        "drawdown": 5.04,
        "realized": -3.25,
        "unrealized": -78.9,
    }
