from pathlib import Path
import re
import tomllib

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
RANGE = re.compile(r"^[A-Za-z0-9_.-]+>=[0-9][0-9.]*,<[0-9]+$")


def _runtime_dependencies() -> list[str]:
    return tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]


def test_every_runtime_dependency_has_a_floor_and_a_major_ceiling():
    not_ranges = [spec for spec in _runtime_dependencies() if not RANGE.match(spec)]

    assert not_ranges == []


def test_aiohttp_is_not_declared():
    names = [re.split(r"[<>=!~ ]", spec, maxsplit=1)[0].lower() for spec in _runtime_dependencies()]

    assert "aiohttp" not in names
