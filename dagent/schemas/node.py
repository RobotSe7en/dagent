"""DAG node schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from dagent.schemas.invocation import ToolInvocation

NodeStatus = Literal[
    "planned",
    "ready",
    "running",
    "completed",
    "failed",
    "skipped",
]


class DAGNode(BaseModel):
    id: str
    invocation: ToolInvocation
    status: NodeStatus = "planned"

