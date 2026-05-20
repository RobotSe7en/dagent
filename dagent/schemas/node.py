"""DAG node schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from dagent.schemas.capability import CapabilityInvocation

NodeStatus = Literal[
    "planned",
    "ready",
    "running",
    "completed",
    "failed",
    "skipped",
]

NodeType = Literal[
    "capability",
    "start",
]


class DAGNode(BaseModel):
    id: str
    title: str = ""
    goal: str | None = None
    instructions: str | None = None
    invocation: CapabilityInvocation
    node_type: NodeType = "capability"
    status: NodeStatus = "planned"
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)

