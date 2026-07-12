import tomllib
from pathlib import Path

import dagent


def test_public_version_matches_project_metadata() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert dagent.__version__ == pyproject["project"]["version"]
