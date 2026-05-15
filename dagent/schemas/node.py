"""DAG node schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
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
    invocation: CapabilityInvocation
    node_type: NodeType = "capability"
    status: NodeStatus = "planned"

