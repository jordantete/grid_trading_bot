import dataclasses
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import re
from typing import Any, get_type_hints

import pytest

from grid_trading_bot.config.schema import BotConfig

DOC = Path(__file__).resolve().parents[2] / "docs" / "configuration" / "config-file.md"
COMPUTED_DEFAULTS = {"persistence.db_path"}


@dataclass(frozen=True)
class Row:
    section: str
    key: str
    default: Any
    bounds: Any
    is_computed: bool

    @property
    def id(self) -> str:
        return f"{self.section}.{self.key}"


def _parse_default(cell: str) -> Any:
    cell = cell.strip()
    if cell == "none":
        return None
    return json.loads(cell.strip("`"))


def _number(text: str) -> float | int:
    return float(text) if "." in text else int(text)


def _parse_bounds(cell: str) -> Any:
    """Return ("range", low, high, low_exclusive), ("choices", {...}) or None for an unconstrained or word range."""
    cell = cell.strip()
    if not cell or cell in {"non-empty", "true or false"} or cell == "`true` or `false`":
        return None
    if "\u2013" in cell:
        low, high = cell.split("\u2013")
        return ("range", _number(low), _number(high), False)
    if cell.startswith("above "):
        return ("range", _number(cell.removeprefix("above ")), None, True)
    if cell.endswith(" or more"):
        return ("range", _number(cell.removesuffix(" or more")), None, False)
    choices = re.fullmatch(r"`(\w+)` or `(\w+)`", cell)
    if choices:
        return ("choices", frozenset(choices.groups()))
    raise AssertionError(f"unparsed range cell: {cell!r}")


def _documented_rows() -> list[Row]:
    rows: list[Row] = []
    section: str | None = None
    in_table = False
    for line in DOC.read_text().splitlines():
        if line.startswith(("###", "####")):
            token = re.search(r"`([\w.]+)`", line)
            section = token.group(1) if token else None
            in_table = False
        elif section and line.startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells[0] == "Parameter":
                in_table = "Default" in cells
            elif in_table and not set(cells[0]) <= {"-"}:
                key = cells[0].strip("`")
                rows.append(
                    Row(
                        section=section,
                        key=key,
                        default=None if f"{section}.{key}" in COMPUTED_DEFAULTS else _parse_default(cells[2]),
                        bounds=_parse_bounds(cells[3]),
                        is_computed=f"{section}.{key}" in COMPUTED_DEFAULTS,
                    )
                )
    return rows


ROWS = _documented_rows()
SECTIONS = sorted({row.section for row in ROWS})


def _section_class(path: str) -> tuple[type, dataclasses.Field[Any] | None, type | None]:
    """Walk BotConfig by a dotted path; return the section class, its field and its parent class."""
    cls: type = BotConfig
    parent: type | None = None
    field: dataclasses.Field[Any] | None = None
    for name in path.split("."):
        field = next(f for f in dataclasses.fields(cls) if f.name == name)
        hint = get_type_hints(cls)[name]
        parent, cls = cls, next((a for a in getattr(hint, "__args__", (hint,)) if dataclasses.is_dataclass(a)), hint)
    return cls, field, parent


def _default_instance(path: str) -> Any:
    _, field, _ = _section_class(path)
    assert field is not None
    return field.default_factory()  # type: ignore[misc]


def schema_default(section: str, key: str) -> Any:
    value = getattr(_default_instance(section), key)
    return value.value if isinstance(value, Enum) else value


def schema_bounds(section: str, key: str) -> Any:
    cls, _, _ = _section_class(section)
    field = next(f for f in dataclasses.fields(cls) if f.name == key)
    hint = get_type_hints(cls)[key]
    if isinstance(hint, type) and issubclass(hint, Enum):
        return ("choices", frozenset(member.value for member in hint))
    if "choices" in field.metadata:
        return ("choices", frozenset(field.metadata["choices"]))
    if "range" in field.metadata:
        low, high = field.metadata["range"]
        return ("range", low, high, bool(field.metadata.get("low_exclusive")))
    return None


def test_the_four_documented_sections_are_found():
    assert SECTIONS == [
        "execution",
        "grid_strategy.dynamic_spacing",
        "persistence",
        "risk_management.trailing_stop_loss",
    ]


@pytest.mark.parametrize("row", [row for row in ROWS if not row.is_computed], ids=lambda row: row.id)
def test_every_documented_default_matches_the_schema(row):
    assert schema_default(row.section, row.key) == row.default


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row.id)
def test_every_documented_range_matches_the_schema(row):
    assert schema_bounds(row.section, row.key) == row.bounds


@pytest.mark.parametrize("section", SECTIONS)
def test_every_schema_field_of_a_documented_section_is_documented(section):
    cls, _, _ = _section_class(section)
    documented = {row.key for row in ROWS if row.section == section}
    assert {f.name for f in dataclasses.fields(cls)} == documented


def test_max_slippage_is_not_in_a_table():
    assert "max_slippage" not in {row.key for row in ROWS}


def test_max_slippage_is_named_as_a_refused_key():
    assert "`execution.max_slippage`" in DOC.read_text()
