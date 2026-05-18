"""DAG schema."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, computed_field

from dagent.schemas.artifact import Artifact
from dagent.schemas.edge import DAGEdge
from dagent.schemas.node import DAGNode
from dagent.schemas.run_trace import RunTrace


DAGStatus = Literal[
    "draft",
    "review_required",
    "approved",
    "running",
    "completed",
    "failed",
    "aborted",
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
    title: str = ""
    goal: str | None = None
    instructions: str | None = None
    tool: str | None = None
    args: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)


class PlanSpec(BaseModel):
    task: str = ""
    nodes: list[PlanNodeSpec] = Field(default_factory=list)


DAGRunStatus = Literal[
    "planned",
    "running",
    "completed",
    "failed",
]


class DAGSpec(BaseModel):
    id: str
    name: str
    version: int = 1
    description: str = ""
    input_schema: dict = Field(default_factory=dict)
    artifacts: dict[str, Artifact] = Field(default_factory=dict)
    nodes: list[DAGNode] = Field(default_factory=list)
    edges: list[DAGEdge] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class DAGRun(BaseModel):
    run_id: str
    spec_id: str | None = None
    workspace_path: str
    dag: DAG
    trace: RunTrace

    @computed_field
    @property
    def status(self) -> DAGRunStatus:
        if self.trace.status == "completed":
            return "completed"
        if self.trace.status == "failed":
            return "failed"
        if self.trace.status == "running":
            return "running"
        return "planned"
