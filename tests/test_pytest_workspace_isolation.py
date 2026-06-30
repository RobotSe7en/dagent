import asyncio
import shutil
from pathlib import Path

import dagent
from dagent.capabilities.catalog import CapabilityCatalog
from dagent.providers import ChatResponse, MockProvider


def run(coro):
    return asyncio.run(coro)


def test_default_runner_does_not_create_repo_run_workspace() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    repo_runs = repo_root / ".dagent" / "runs"
    before = _run_workspace_names(repo_runs)
    result = None

    try:
        runner = dagent.Runner(provider=MockProvider([ChatResponse(content="done")]))
        result = run(
            runner.run(
                dagent.ToolAgent(profile="conversation"),
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        runner.close()
    finally:
        after = _run_workspace_names(repo_runs)
        for name in after - before:
            shutil.rmtree(repo_runs / name, ignore_errors=True)

    assert result is not None
    assert _run_workspace_names(repo_runs) == before
    assert not Path(result.workspace_path).resolve().is_relative_to(repo_runs.resolve())


def test_default_capability_catalog_workspace_is_isolated_from_repo_root() -> None:
    repo_workspace = Path(__file__).resolve().parents[1] / ".dagent"

    catalog = CapabilityCatalog()

    assert not catalog.workspace_root.is_relative_to(repo_workspace.resolve())


def _run_workspace_names(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.name for path in root.iterdir() if path.is_dir()}
