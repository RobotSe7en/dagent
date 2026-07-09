"""Artifact workspace helpers for DAG runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from posixpath import normpath
from uuid import uuid4

from dagent.config import DEFAULT_RUNS_DIR
from dagent.schemas import Artifact, ArtifactState, DAGNode
from dagent.schemas.run_id import validate_run_id


class ArtifactPathError(ValueError):
    """Raised when an artifact path cannot be safely rooted in a run workspace."""


@dataclass(frozen=True)
class ArtifactUpload:
    """Uploaded file content associated with a DAGSpec artifact."""

    filename: str
    content: bytes


WORKBENCH_UPLOAD_ROOT = "uploads"


def create_run_workspace(root: str | Path = DEFAULT_RUNS_DIR, *, run_id: str | None = None) -> Path:
    workspace_name = run_id if run_id is not None else f"run_{uuid4().hex}"
    validate_run_id(workspace_name)
    workspace = Path(root).resolve() / workspace_name
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


def materialize_artifact_uploads(
    uploads: Mapping[str, Sequence[ArtifactUpload]],
    *,
    artifacts: dict[str, Artifact],
    workspace_path: str | Path,
) -> set[str]:
    """Write uploaded artifact files into a run workspace."""

    materialized: set[str] = set()
    for artifact_id, artifact_uploads in uploads.items():
        if not artifact_uploads:
            continue
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            continue
        target_paths = resolve_artifact_paths(artifact, workspace_path)
        target_root = target_paths[0]
        as_directory = (
            len(artifact_uploads) > 1
            or _artifact_path_is_directory_like(artifact.paths[0])
            or any(_upload_filename_has_parent(upload.filename) for upload in artifact_uploads)
        )
        if as_directory:
            for upload in artifact_uploads:
                destination = (target_root / _safe_upload_filename(upload.filename)).resolve()
                _ensure_within_workspace(destination, workspace_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(upload.content)
        else:
            _ensure_within_workspace(target_root, workspace_path)
            target_root.parent.mkdir(parents=True, exist_ok=True)
            target_root.write_bytes(artifact_uploads[0].content)
        materialized.add(artifact_id)
    return materialized


def materialize_workbench_uploads(
    uploads: Sequence[ArtifactUpload],
    *,
    workspace_path: str | Path,
    upload_root: str = WORKBENCH_UPLOAD_ROOT,
) -> list[str]:
    """Write smart workbench input uploads into a run workspace."""

    if not uploads:
        return []
    _validate_artifact_path(upload_root)
    workspace = Path(workspace_path).resolve()
    target_root = (workspace / upload_root).resolve()
    _ensure_within_workspace(target_root, workspace)

    materialized: list[str] = []
    for upload in uploads:
        relative_path = _safe_upload_filename(upload.filename)
        destination = (target_root / relative_path).resolve()
        _ensure_within_workspace(destination, workspace)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(upload.content)
        materialized.append(str(Path(upload_root) / relative_path))
    return materialized


def validate_upload_filename(filename: str) -> None:
    """Validate a browser-provided upload filename without writing it."""

    _safe_upload_filename(filename)


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


def _artifact_path_is_directory_like(path: str) -> bool:
    return path.endswith(("/", "\\"))


def _upload_filename_has_parent(filename: str) -> bool:
    parts = _safe_upload_filename(filename).parts
    return len(parts) > 1


def _safe_upload_filename(filename: str) -> Path:
    slash_normalized = filename.replace("\\", "/")
    if not filename.strip():
        raise ArtifactPathError("Uploaded file name cannot be empty.")
    windows_raw_candidate = PureWindowsPath(filename)
    if slash_normalized.startswith("/") or windows_raw_candidate.is_absolute() or windows_raw_candidate.drive:
        raise ArtifactPathError(f"Uploaded file name '{filename}' must be relative.")
    if ".." in slash_normalized.split("/"):
        raise ArtifactPathError(f"Uploaded file name '{filename}' cannot contain '..'.")

    normalized = normpath(slash_normalized)
    if not normalized or normalized == ".":
        raise ArtifactPathError("Uploaded file name cannot be empty.")
    candidate = Path(normalized)
    windows_candidate = PureWindowsPath(normalized)
    if candidate.is_absolute() or windows_candidate.is_absolute() or windows_candidate.drive:
        raise ArtifactPathError(f"Uploaded file name '{filename}' must be relative.")
    if ".." in candidate.parts:
        raise ArtifactPathError(f"Uploaded file name '{filename}' cannot contain '..'.")
    return candidate


def _ensure_within_workspace(path: Path, workspace_path: str | Path) -> None:
    workspace = Path(workspace_path).resolve()
    try:
        path.resolve().relative_to(workspace)
    except ValueError as exc:
        raise ArtifactPathError(
            f"Uploaded artifact path '{path}' escapes workspace '{workspace}'."
        ) from exc
