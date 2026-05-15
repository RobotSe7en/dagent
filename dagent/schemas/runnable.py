"""Runnable capability schemas."""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from dagent.schemas.common import Boundary, RiskLevel


RunnableKind = Literal[
    "tool",
    "mcp",
    "skill",
    "shell",
    "custom_tool",
    "agent",
    "memory",
    "file",
]
RunnableStatus = Literal["completed", "failed"]


class RunnablePolicy(BaseModel):
    risk: RiskLevel = "low"
    requires_review: bool = False
    sandbox_required: bool = False
    network: bool = False
    secrets: list[str] = Field(default_factory=list)


class RunnableDefinition(BaseModel):
    id: str
    name: str
    kind: RunnableKind
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    policy: RunnablePolicy = Field(default_factory=RunnablePolicy)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class RunnableInvocation(BaseModel):
    invocation_id: str = Field(default_factory=lambda: f"run_inv_{uuid4().hex}")
    runnable_id: str
    kind: RunnableKind
    arguments: dict[str, Any] = Field(default_factory=dict)
    boundary: Boundary = Field(default_factory=Boundary)
    risk: RiskLevel = "low"


class RunnableResult(BaseModel):
    invocation_id: str
    runnable_id: str
    kind: RunnableKind
    status: RunnableStatus
    content: str = ""
    error: str | None = None
    stop_reason: str = "completed"
    stdout: str = ""
    stderr: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    policy_decision: dict[str, Any] = Field(default_factory=dict)
    trace_events: list[dict[str, Any]] = Field(default_factory=list)


class RunnableRuntime(BaseModel):
    sandbox: str = "local"
    workspace_id: str | None = None
    session_id: str | None = None
