"""DAG node and execution boundary schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


BoundaryMode = Literal["read_only", "write_limited", "full"]
NodeKind = Literal["agent", "tool"]
NodeStatus = Literal[
    "planned",
    "ready",
    "running",
    "blocked_permission",
    "completed",
    "failed",
    "skipped",
]
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
    kind: NodeKind = "agent"
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    agent: str | None = None
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    boundary: Boundary = Field(default_factory=Boundary)
    risk: RiskLevel = "low"
    risk_reason: str = ""
    expected_output: str = ""
    max_steps: int = 8
    timeout_seconds: int = 300
    status: NodeStatus = "planned"

    @model_validator(mode="after")
    def normalize_tool_node(self) -> "DAGNode":
        if self.tool:
            self.kind = "tool"
            if self.tool not in self.tools:
                self.tools = [self.tool, *self.tools]
            self.max_steps = min(self.max_steps, 1)
        elif self.kind == "tool" and self.tools:
            self.tool = self.tools[0]
            self.max_steps = min(self.max_steps, 1)
        return self

