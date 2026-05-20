"""Capability schemas."""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from dagent.schemas.common import Boundary, RiskLevel


CapabilityKind = Literal[
    "tool",
    "mcp",
    "skill",
    "shell",
    "custom_tool",
    "agent",
    "memory",
    "file",
]
CapabilityStatus = Literal["completed", "failed"]


class CapabilityPolicy(BaseModel):
    risk: RiskLevel = "low"
    requires_review: bool = False
    sandbox_required: bool = False
    network: bool = False
    secrets: list[str] = Field(default_factory=list)


class CapabilityDefinition(BaseModel):
    id: str
    name: str
    kind: CapabilityKind
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    policy: CapabilityPolicy = Field(default_factory=CapabilityPolicy)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class CapabilityInvocation(BaseModel):
    invocation_id: str = Field(default_factory=lambda: f"run_inv_{uuid4().hex}")
    capability_id: str
    kind: CapabilityKind
    arguments: dict[str, Any] = Field(default_factory=dict)
    boundary: Boundary = Field(default_factory=Boundary)
    risk: RiskLevel = "low"


class CapabilityResult(BaseModel):
    invocation_id: str
    capability_id: str
    kind: CapabilityKind
    status: CapabilityStatus
    content: str = ""
    error: str | None = None
    stop_reason: str = "completed"
    stdout: str = ""
    stderr: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    policy_decision: dict[str, Any] = Field(default_factory=dict)
    trace: dict[str, Any] | None = None


class CapabilityRuntime(BaseModel):
    sandbox: str = "local"
    workspace_id: str | None = None
    session_id: str | None = None
