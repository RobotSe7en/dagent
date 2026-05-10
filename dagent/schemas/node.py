"""DAG node and execution boundary schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


BoundaryMode = Literal["read_only", "write_limited", "full"]
NodeStatus = Literal[
    "planned",
    "ready",
    "running",
    "completed",
    "failed",
    "skipped",
]
RiskLevel = Literal["low", "medium", "high"]


class Boundary(BaseModel):
    mode: BoundaryMode = "read_only"
    allowed_paths: list[str] = Field(default_factory=list)
    allowed_commands: list[str] = Field(default_factory=list)


class DAGNode(BaseModel):
    id: str
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    boundary: Boundary = Field(default_factory=Boundary)
    risk: RiskLevel = "low"
    status: NodeStatus = "planned"

