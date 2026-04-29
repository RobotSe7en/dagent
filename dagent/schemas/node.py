"""DAG node and execution boundary schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


BoundaryMode = Literal["read_only", "write_limited", "full"]
RiskLevel = Literal["low", "medium", "high"]


class Boundary(BaseModel):
    mode: BoundaryMode = "read_only"
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    allowed_commands: list[str] = Field(default_factory=list)
    forbidden_commands: list[str] = Field(default_factory=list)


class DAGNode(BaseModel):
    id: str
    title: str
    goal: str
    agent: str | None = None
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    boundary: Boundary = Field(default_factory=Boundary)
    risk: RiskLevel = "low"
    risk_reason: str = ""
    expected_output: str = ""
    max_steps: int = 8
    timeout_seconds: int = 300

