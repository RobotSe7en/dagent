"""Artifact workspace helpers for DAG runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from posixpath import normpath
from uuid import uuid4

from dagent.schemas import Artifact, ArtifactFileManifest, ArtifactFileRef, ArtifactState, DAGNode
from dagent.schemas.run_id import validate_run_id
from dagent.config import DEFAULT_RUNS_DIR


class ArtifactPathError(ValueError):
    """Raised when an artifact path cannot be safely rooted in a run workspace."""


@dataclass(frozen=True)
class ArtifactUpload:
    """Uploaded file content associated with a DAGSpec artifact."""

    filename: str
    content: bytes
    media_type: str | None = None


WORKBENCH_UPLOAD_ROOT = "uploads"
MAX_ARTIFACT_UPLOAD_FILES = 256
MAX_ARTIFACT_UPLOAD_FILE_BYTES = 25 * 1024 * 1024
MAX_ARTIFACT_UPLOAD_TOTAL_BYTES = 100 * 1024 * 1024


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
) -> tuple[ArtifactFileManifest, ...]:
    """Write uploads and return their immutable, deterministic file manifests.

    The returned paths are always workspace-relative. They are derived only from
    this call's uploads, never from a workspace scan.
    """

    workspace = Path(workspace_path).resolve()
    planned: list[_PlannedArtifactUpload] = []
    claimed_targets: dict[Path, tuple[str, str]] = {}
    total_files = 0
    total_bytes = 0

    for artifact_id in sorted(uploads):
        artifact_uploads = uploads[artifact_id]
        if not artifact_uploads:
            continue
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            continue
        validate_artifact_paths(artifact.paths)
        declared_target_root = workspace / artifact.paths[0]
        _reject_symlinked_upload_path(declared_target_root, workspace)
        target_paths = resolve_artifact_paths(artifact, workspace)
        target_root = target_paths[0]
        as_directory = (
            len(artifact_uploads) > 1
            or _artifact_path_is_directory_like(artifact.paths[0])
            or any(_upload_filename_has_parent(upload.filename) for upload in artifact_uploads)
        )
        for upload in artifact_uploads:
            _validate_artifact_upload_size(upload)
            total_files += 1
            total_bytes += len(upload.content)
            if total_files > MAX_ARTIFACT_UPLOAD_FILES:
                raise ArtifactPathError(
                    "Artifact uploads exceed "
                    f"MAX_ARTIFACT_UPLOAD_FILES={MAX_ARTIFACT_UPLOAD_FILES}."
                )
            if total_bytes > MAX_ARTIFACT_UPLOAD_TOTAL_BYTES:
                raise ArtifactPathError(
                    "Artifact uploads exceed "
                    f"MAX_ARTIFACT_UPLOAD_TOTAL_BYTES={MAX_ARTIFACT_UPLOAD_TOTAL_BYTES}."
                )
            destination = (
                target_root / _safe_upload_filename(upload.filename)
                if as_directory
                else target_root
            )
            _ensure_within_artifact_target(
                destination,
                target_root=target_root,
                workspace_path=workspace,
            )
            resolved_destination = destination.resolve()
            existing = claimed_targets.get(resolved_destination)
            if existing is not None:
                existing_artifact_id, existing_filename = existing
                raise ArtifactPathError(
                    f"Uploaded artifact target '{destination.relative_to(workspace)}' is duplicated "
                    f"by '{existing_artifact_id}:{existing_filename}' and "
                    f"'{artifact_id}:{upload.filename}'."
                )
            claimed_targets[resolved_destination] = (artifact_id, upload.filename)
            relative_path = destination.relative_to(workspace).as_posix()
            planned.append(
                _PlannedArtifactUpload(
                    artifact_id=artifact_id,
                    destination=destination,
                    upload=upload,
                    file=ArtifactFileRef(
                        path=relative_path,
                        name=destination.name,
                        size=len(upload.content),
                        media_type=upload.media_type,
                    ),
                )
            )

    _validate_no_overlapping_upload_targets(planned)
    for item in sorted(planned, key=lambda item: item.file.path):
        item.destination.parent.mkdir(parents=True, exist_ok=True)
        item.destination.write_bytes(item.upload.content)

    manifests: list[ArtifactFileManifest] = []
    for artifact_id in sorted({item.artifact_id for item in planned}):
        files = tuple(
            item.file
            for item in sorted(
                (item for item in planned if item.artifact_id == artifact_id),
                key=lambda item: item.file.path,
            )
        )
        manifests.append(ArtifactFileManifest(artifact_id=artifact_id, files=files))
    return tuple(manifests)


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


@dataclass(frozen=True)
class _PlannedArtifactUpload:
    artifact_id: str
    destination: Path
    upload: ArtifactUpload
    file: ArtifactFileRef


def _validate_artifact_upload_size(upload: ArtifactUpload) -> None:
    if len(upload.content) > MAX_ARTIFACT_UPLOAD_FILE_BYTES:
        raise ArtifactPathError(
            f"Uploaded artifact file '{upload.filename}' exceeds "
            f"MAX_ARTIFACT_UPLOAD_FILE_BYTES={MAX_ARTIFACT_UPLOAD_FILE_BYTES}."
        )


def _validate_no_overlapping_upload_targets(
    planned: list[_PlannedArtifactUpload],
) -> None:
    for index, item in enumerate(planned):
        for other in planned[index + 1:]:
            if (
                item.destination in other.destination.parents
                or other.destination in item.destination.parents
            ):
                raise ArtifactPathError(
                    "Uploaded artifact targets overlap: "
                    f"'{item.file.path}' and '{other.file.path}'."
                )


def _ensure_within_artifact_target(
    destination: Path,
    *,
    target_root: Path,
    workspace_path: str | Path,
) -> None:
    _ensure_within_workspace(destination, workspace_path)
    try:
        destination.relative_to(target_root)
    except ValueError as exc:
        raise ArtifactPathError(
            f"Uploaded artifact path '{destination}' escapes declared artifact target "
            f"'{target_root}'."
        ) from exc
    _reject_symlinked_upload_path(destination, workspace_path)


def _reject_symlinked_upload_path(path: Path, workspace_path: str | Path) -> None:
    workspace = Path(workspace_path).resolve()
    try:
        relative = path.relative_to(workspace)
    except ValueError as exc:
        raise ArtifactPathError(
            f"Uploaded artifact path '{path}' escapes workspace '{workspace}'."
        ) from exc
    current = workspace
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ArtifactPathError(
                f"Uploaded artifact path '{path}' crosses symlink '{current}'."
            )


def _ensure_within_workspace(path: Path, workspace_path: str | Path) -> None:
    workspace = Path(workspace_path).resolve()
    try:
        path.resolve().relative_to(workspace)
    except ValueError as exc:
        raise ArtifactPathError(
            f"Uploaded artifact path '{path}' escapes workspace '{workspace}'."
        ) from exc
