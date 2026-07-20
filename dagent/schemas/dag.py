"""DAG schema."""

from __future__ import annotations

from typing import Any, Iterable, Iterator, Literal

from pydantic import BaseModel, Field, computed_field

from dagent.schemas.artifact import Artifact
from dagent.schemas.capability import CapabilityInvocation
from dagent.schemas.edge import DAGEdge
from dagent.schemas.node import (
    CapabilityNodePayload,
    DAGNode,
    LoopNodePayload,
    MapNodePayload,
    SubgraphNodePayload,
)
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
    output: Any = None
    metadata: dict = Field(default_factory=dict)


SubgraphNodePayload.model_rebuild(_types_namespace={"DAGSpec": DAGSpec})
LoopNodePayload.model_rebuild(_types_namespace={"DAGSpec": DAGSpec})
DAGNode.model_rebuild(_types_namespace={"DAGSpec": DAGSpec})


def iter_dag_invocations(nodes: Iterable[DAGNode]) -> Iterator[CapabilityInvocation]:
    """Yield every capability invocation reachable from ``nodes``, including nested specs."""
    for node in nodes:
        payload = node.payload
        if isinstance(payload, (CapabilityNodePayload, MapNodePayload)):
            yield payload.invocation
        elif isinstance(payload, SubgraphNodePayload):
            yield from iter_dag_invocations(payload.spec.nodes)
        elif isinstance(payload, LoopNodePayload):
            yield from iter_dag_invocations(payload.body.nodes)


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
