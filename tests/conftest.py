import shutil
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_RUNS = _REPO_ROOT / ".dagent" / "runs"


@pytest.fixture(autouse=True)
def cleanup_repo_default_run_workspaces():
    before = _run_workspace_names(_REPO_RUNS)
    yield
    after = _run_workspace_names(_REPO_RUNS)
    for name in after - before:
        shutil.rmtree(_REPO_RUNS / name, ignore_errors=True)


def _run_workspace_names(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.name for path in root.iterdir() if path.is_dir()}
