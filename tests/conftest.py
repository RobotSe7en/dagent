from pathlib import Path

import pytest

import dagent.capabilities.catalog as catalog_module
import dagent.runner as runner_module
from dagent.config import DEFAULT_WORKSPACE


_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate_default_runner_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_runner_init = runner_module.Runner.__init__
    original_catalog_init = catalog_module.CapabilityCatalog.__init__

    def isolated_runner_init(self, *args, **kwargs):
        if _uses_repo_default_workspace(kwargs):
            workspace = tmp_path / DEFAULT_WORKSPACE
            workspace.mkdir(parents=True, exist_ok=True)
            kwargs = {**kwargs, "workspace": workspace}
        return original_runner_init(self, *args, **kwargs)

    def isolated_catalog_init(self, *args, **kwargs):
        if _uses_repo_default_workspace_root(kwargs):
            workspace = tmp_path / DEFAULT_WORKSPACE
            workspace.mkdir(parents=True, exist_ok=True)
            kwargs = {**kwargs, "workspace_root": workspace}
        return original_catalog_init(self, *args, **kwargs)

    monkeypatch.setattr(runner_module.Runner, "__init__", isolated_runner_init)
    monkeypatch.setattr(catalog_module.CapabilityCatalog, "__init__", isolated_catalog_init)


def _uses_repo_default_workspace(kwargs: dict[str, object]) -> bool:
    if Path.cwd().resolve() != _REPO_ROOT:
        return False
    workspace = kwargs.get("workspace", DEFAULT_WORKSPACE)
    return Path(workspace) == Path(DEFAULT_WORKSPACE)


def _uses_repo_default_workspace_root(kwargs: dict[str, object]) -> bool:
    if Path.cwd().resolve() != _REPO_ROOT:
        return False
    workspace = kwargs.get("workspace_root", DEFAULT_WORKSPACE)
    return Path(workspace) == Path(DEFAULT_WORKSPACE)
