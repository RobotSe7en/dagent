"""Artifact workspace helpers for DAG runs."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
from uuid import uuid4

from dagent.schemas import Artifact, ArtifactState, DAGNode


class ArtifactPathError(ValueError):
    """Raised when an artifact path cannot be safely rooted in a run workspace."""


def create_run_workspace(root: str | Path = ".dagent-runs") -> Path:
    workspace = Path(root).resolve() / f"run_{uuid4().hex}"
    workspace.mkdir(parents=True, exist_ok=False)
    return workspace


def init_artifact_states(artifacts: dict[str, Artifact]) -> dict[str, ArtifactState]:
    return {
        artifact_id: ArtifactState(id=artifact.id, paths=list(artifact.paths))
        for artifact_id, artifact in artifacts.items()
    }


def validate_artifact_paths(paths: list[str]) -> None:
    if not paths:
        raise ArtifactPathError("Artifact paths must contain at least one path.")
    for path in paths:
        _validate_artifact_path(path)


def resolve_artifact_paths(artifact: Artifact, workspace_path: str | Path) -> list[Path]:
    validate_artifact_paths(artifact.paths)
    workspace = Path(workspace_path).resolve()
    resolved_paths = [(workspace / path).resolve() for path in artifact.paths]
    for path in resolved_paths:
        try:
            path.relative_to(workspace)
        except ValueError as exc:
            raise ArtifactPathError(
                f"Artifact path '{path}' escapes workspace '{workspace}'."
            ) from exc
    return resolved_paths


def resolve_node_artifacts(
    node: DAGNode,
    *,
    artifacts: dict[str, Artifact],
    workspace_path: str | Path,
) -> tuple[dict[str, list[Path]], dict[str, list[Path]]]:
    """Resolve a node's input and output artifact ids to workspace paths."""

    return (
        _resolve_artifact_ids(node.inputs, artifacts=artifacts, workspace_path=workspace_path),
        _resolve_artifact_ids(node.outputs, artifacts=artifacts, workspace_path=workspace_path),
    )


def update_node_output_artifacts(
    node: DAGNode,
    *,
    artifacts: dict[str, Artifact],
    states: dict[str, ArtifactState],
    workspace_path: str | Path,
) -> None:
    for artifact_id in node.outputs:
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            continue
        paths = resolve_artifact_paths(artifact, workspace_path)
        exists = all(path.exists() for path in paths)
        states[artifact_id] = ArtifactState(
            id=artifact.id,
            paths=list(artifact.paths),
            status="created" if exists else "missing",
            producer_node_id=node.id,
            error=None if exists else "One or more artifact paths were not created.",
        )


def _resolve_artifact_ids(
    artifact_ids: list[str],
    *,
    artifacts: dict[str, Artifact],
    workspace_path: str | Path,
) -> dict[str, list[Path]]:
    resolved: dict[str, list[Path]] = {}
    for artifact_id in artifact_ids:
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            continue
        resolved[artifact_id] = resolve_artifact_paths(artifact, workspace_path)
    return resolved


def _validate_artifact_path(path: str) -> None:
    candidate = Path(path)
    windows_candidate = PureWindowsPath(path)
    if candidate.is_absolute() or windows_candidate.is_absolute() or windows_candidate.drive:
        raise ArtifactPathError(f"Artifact path '{path}' must be relative.")
    if not path.strip():
        raise ArtifactPathError("Artifact path cannot be empty.")
    if ".." in candidate.parts:
        raise ArtifactPathError(f"Artifact path '{path}' cannot contain '..'.")
