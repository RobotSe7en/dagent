"""Capability schemas."""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from dagent.schemas.common import Boundary, RiskLevel


CapabilityKind = Literal[
    "tool",
    "mcp",
    "skill",
    "agent",
    "memory",
]
CapabilityStatus = Literal["completed", "failed"]
_CAPABILITY_ID_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_]+$")
_CAPABILITY_ID_SEGMENT_COUNTS = {
    "tool": 2,
    "agent": 2,
    "mcp": 3,
    "skill": 2,
    "memory": 2,
}


def validate_capability_id(
    value: Any,
    *,
    kind: CapabilityKind | None = None,
    label: str = "Capability ids",
) -> str:
    capability_id = str(value or "")
    if capability_id != capability_id.strip():
        raise ValueError(f"{label} may not contain leading or trailing whitespace.")
    parts = capability_id.split(".")
    if not parts or any(not part for part in parts):
        raise ValueError(f"{label} must use a supported dotted capability id form.")
    prefix = parts[0]
    if prefix not in _CAPABILITY_ID_SEGMENT_COUNTS:
        raise ValueError(f"{label} must start with tool, agent, mcp, skill, or memory.")
    if kind is not None and prefix != kind:
        raise ValueError(f"{label} for kind '{kind}' must start with '{kind}.'.")
    expected_count = _CAPABILITY_ID_SEGMENT_COUNTS[prefix]
    if len(parts) != expected_count:
        raise ValueError(f"{label} must use the form '{_capability_id_form(prefix)}'.")
    for part in parts:
        validate_capability_id_segment(part, label=label)
    return capability_id


def validate_capability_id_segment(value: Any, *, label: str = "Capability id segments") -> str:
    segment = str(value or "")
    if segment != segment.strip():
        raise ValueError(f"{label} may not contain leading or trailing whitespace.")
    if not _CAPABILITY_ID_SEGMENT_RE.fullmatch(segment):
        raise ValueError(f"{label} may contain only letters, numbers, and underscores.")
    return segment


def _capability_id_form(prefix: str) -> str:
    if prefix == "mcp":
        return "mcp.<server>.<tool>"
    return f"{prefix}.<name>"


class CapabilityPolicy(BaseModel):
    risk: RiskLevel = "low"
    requires_review: bool = False
    sandbox_required: bool = False
    network: bool = False
    secrets: list[str] = Field(default_factory=list)


class CapabilityDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: CapabilityKind
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=dict)
    policy: CapabilityPolicy = Field(default_factory=CapabilityPolicy)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class CapabilityInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    value: Any = None
    error: str | None = None
    stop_reason: str = "completed"
    stdout: str = ""
    stderr: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    policy_decision: dict[str, Any] = Field(default_factory=dict)
    trace: dict[str, Any] | None = None

    @classmethod
    def completed(
        cls,
        invocation: "CapabilityInvocation",
        content: str = "",
        *,
        value: Any = None,
        **fields: Any,
    ) -> "CapabilityResult":
        return cls(
            invocation_id=invocation.invocation_id,
            capability_id=invocation.capability_id,
            kind=invocation.kind,
            status="completed",
            content=content,
            value=value,
            **fields,
        )

    @classmethod
    def failed(
        cls,
        invocation: "CapabilityInvocation",
        error: str,
        *,
        stop_reason: str,
        **fields: Any,
    ) -> "CapabilityResult":
        return cls(
            invocation_id=invocation.invocation_id,
            capability_id=invocation.capability_id,
            kind=invocation.kind,
            status="failed",
            error=error,
            stop_reason=stop_reason,
            **fields,
        )
