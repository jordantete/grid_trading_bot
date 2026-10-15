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
QUERIES = _queries()


@pytest.mark.parametrize(("panel", "expr"), QUERIES, ids=[f"{panel}-{i}" for i, (panel, _) in enumerate(QUERIES)])
def test_dashboard_filters_match_source_text(panel, expr):
    missing = [
        text
        for text in _required_texts(expr)
        if text not in CLASS_NAMES and not any(text in fragment for fragment in FRAGMENTS)
    ]
    assert not missing, f"Panel {panel!r} filters on text that no longer exists in src/: {missing}"


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
