from pathlib import Path
import tomllib

import grid_trading_bot

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_the_package_version_is_the_pyproject_version():
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["version"]

    assert grid_trading_bot.__version__ == declared
