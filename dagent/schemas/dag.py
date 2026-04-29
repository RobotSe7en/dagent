"""DAG schema."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from dagent.schemas.edge import DAGEdge
from dagent.schemas.node import DAGNode


DAGStatus = Literal[
    "draft",
    "review_required",
    "approved",
    "running",
    "completed",
    "failed",
]


class DAG(BaseModel):
    dag_id: str
    task_id: str
    version: int = 1
    status: DAGStatus = "draft"
    nodes: list[DAGNode] = Field(default_factory=list)
    edges: list[DAGEdge] = Field(default_factory=list)


class PlanNodeSpec(BaseModel):
    id: str
    goal: str
    tool: str | None = None
    args: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    risk: str | None = None
    review_reason: str | None = None


class PlanSpec(BaseModel):
    task: str = ""
    nodes: list[PlanNodeSpec] = Field(default_factory=list)
