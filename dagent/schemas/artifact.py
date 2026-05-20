"""Artifact schemas for DAG runs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ArtifactStatus = Literal["planned", "created", "missing", "failed"]


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
