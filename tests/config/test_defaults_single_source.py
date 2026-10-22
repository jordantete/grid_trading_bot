import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

from grid_trading_bot.config.schema import (
    DynamicSpacingConfig,
    ExecutionConfig,
    PersistenceConfig,
    TrailingStopLossConfig,
)
from grid_trading_bot.core.bot_management.health_check import HealthCheck
from grid_trading_bot.core.order_handling.execution_strategy.live_order_execution_strategy import (
    LiveOrderExecutionStrategy,
)
from grid_trading_bot.core.order_handling.order_status_tracker import OrderStatusTracker
from grid_trading_bot.core.reconciliation.reconciliation_service import ReconciliationService
from grid_trading_bot.core.services.circuit_breaker import CircuitBreaker

SRC = Path(__file__).resolve().parents[2] / "src" / "grid_trading_bot"

# Constructor parameter -> the ExecutionConfig field that owns its default; only 3 of the 9 share the field's name.
CONSTRUCTOR_PARAMETERS = [
    (OrderStatusTracker, "polling_interval", "order_polling_interval"),
    (ReconciliationService, "reconciliation_interval", "reconciliation_interval"),
    (ReconciliationService, "balance_tolerance", "reconciliation_balance_tolerance"),
    (HealthCheck, "check_interval", "health_check_interval"),
    (LiveOrderExecutionStrategy, "max_retries", "max_retries"),
    (LiveOrderExecutionStrategy, "retry_delay", "retry_delay"),
    (CircuitBreaker, "failure_threshold", "circuit_breaker_failure_threshold"),
    (CircuitBreaker, "recovery_timeout", "circuit_breaker_recovery_timeout"),
    (CircuitBreaker, "half_open_max_calls", "circuit_breaker_half_open_max_calls"),
]

SCHEMA_SECTIONS = (ExecutionConfig, PersistenceConfig, DynamicSpacingConfig, TrailingStopLossConfig)


@pytest.mark.parametrize(("cls", "parameter", "field_name"), CONSTRUCTOR_PARAMETERS)
def test_constructors_take_config_values_without_defaults(cls, parameter, field_name):
    assert field_name in {f.name for f in dataclasses.fields(ExecutionConfig)}
    constructor_parameter = inspect.signature(cls.__init__).parameters[parameter]
    assert constructor_parameter.default is inspect.Parameter.empty
    assert constructor_parameter.kind is inspect.Parameter.KEYWORD_ONLY


def _parameters_with_default(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, ast.expr]]:
    arguments = function.args
    positional = arguments.posonlyargs + arguments.args
    with_default = list(zip(positional[len(positional) - len(arguments.defaults) :], arguments.defaults, strict=True))
    with_default += [(a, d) for a, d in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True) if d is not None]
    return [(a.arg, d) for a, d in with_default]


def test_no_schema_default_is_repeated_in_src():
    schema_field_names = {f.name for section in SCHEMA_SECTIONS for f in dataclasses.fields(section)}
    constructor_parameters = {(cls.__name__, parameter) for cls, parameter, _ in CONSTRUCTOR_PARAMETERS}
    hits = []
    for path in sorted(SRC.rglob("*.py")):
        if path == SRC / "config" / "schema.py":
            continue
        tree = ast.parse(path.read_text())
        enclosing_class = {
            id(method): node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef) for method in node.body
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            owner = enclosing_class.get(id(node))
            for name, default in _parameters_with_default(node):
                if name in schema_field_names or (owner, name) in constructor_parameters:
                    hits.append(f"{path.relative_to(SRC)}:{default.lineno} {name}={ast.unparse(default)}")
    assert hits == []


def test_no_schema_default_is_hard_coded_at_a_construction_site():
    parameters_by_class: dict[str, set[str]] = {}
    for cls, parameter, _ in CONSTRUCTOR_PARAMETERS:
        parameters_by_class.setdefault(cls.__name__, set()).add(parameter)
    hits = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)):
                continue
            callee = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for keyword in node.keywords:
                if keyword.arg in parameters_by_class.get(callee, ()) and isinstance(keyword.value, ast.Constant):
                    hits.append(f"{path.relative_to(SRC)}:{keyword.value.lineno} {keyword.arg}={keyword.value.value!r}")
    assert hits == []
