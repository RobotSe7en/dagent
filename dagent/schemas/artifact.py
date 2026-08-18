"""Artifact schemas for DAG runs."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ArtifactStatus = Literal["planned", "created", "missing", "failed"]


class ArtifactFileRef(BaseModel):
    """A safe, workspace-relative file materialized for one run input artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    name: str = Field(min_length=1)
    size: int = Field(ge=0)
    media_type: str | None = None

    @field_validator("path")
    @classmethod
    def validate_workspace_relative_path(cls, value: str) -> str:
        _validate_safe_workspace_relative_path(value, label="Artifact file path")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Artifact file name cannot be empty.")
        posix = PurePosixPath(value)
        windows = PureWindowsPath(value)
        if (
            posix.name != value
            or windows.name != value
            or value in {".", ".."}
        ):
            raise ValueError("Artifact file name must not contain a path separator.")
        return value

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("Artifact file media_type cannot be blank.")
        if "\r" in value or "\n" in value:
            raise ValueError("Artifact file media_type cannot contain line breaks.")
        return value

    @model_validator(mode="after")
    def validate_name_matches_path(self) -> "ArtifactFileRef":
        if PurePosixPath(self.path).name != self.name:
            raise ValueError("Artifact file name must match the final path component.")
        return self


class ArtifactFileManifest(BaseModel):
    """Frozen, deterministic input-file snapshot for one declared artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1)
    files: tuple[ArtifactFileRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_order_and_unique_paths(self) -> "ArtifactFileManifest":
        paths = [file.path for file in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("Artifact file manifest paths must be unique.")
        if paths != sorted(paths):
            raise ValueError("Artifact file manifest paths must be sorted.")
        return self


class Artifact(BaseModel):
    """Logical DAG artifact mapped to one or more workspace-relative paths."""

    id: str
    paths: list[str]
    description: str = ""
    required: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactState(BaseModel):
    """Runtime state for one logical artifact in a DAG run."""

    id: str
    paths: list[str]
    status: ArtifactStatus = "planned"
    producer_node_id: str | None = None
    error: str | None = None


def _validate_safe_workspace_relative_path(value: str, *, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} cannot be empty.")
    if "\\" in value:
        raise ValueError(f"{label} must use '/' separators.")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError(f"{label} must be relative.")
    if any(part in {".", ".."} for part in posix.parts):
        raise ValueError(f"{label} cannot contain dot segments.")
