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
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    boundary: Boundary = Field(default_factory=Boundary)
    risk: RiskLevel = "low"
    risk_reason: str = ""
    status: NodeStatus = "planned"
    title: str = Field(default="", exclude=True)
    goal: str = Field(default="", exclude=True)
    kind: NodeKind = Field(default="tool", exclude=True)
    agent: str | None = Field(default=None, exclude=True)
    tools: list[str] = Field(default_factory=list, exclude=True)
    skills: list[str] = Field(default_factory=list, exclude=True)
    expected_output: str = Field(default="", exclude=True)
    max_steps: int = Field(default=1, exclude=True)
    timeout_seconds: int = Field(default=120, exclude=True)

    @model_validator(mode="after")
    def normalize_tool_node(self) -> "DAGNode":
        if not self.title:
            self.title = self.id.replace("_", " ").strip().title() or self.id
        if not self.goal:
            self.goal = f"Run {self.tool}." if self.tool else f"Run node {self.id}."
        if self.tool:
            self.kind = "tool"
            self.tools = [self.tool]
            self.max_steps = 1
        elif self.kind == "tool" and self.tools:
            self.tool = self.tools[0]
            self.max_steps = 1
        return self

