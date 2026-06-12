"""DAG node schemas."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from dagent.schemas.capability import CapabilityInvocation
from dagent.schemas.value import ValueBinding

if TYPE_CHECKING:
    from dagent.schemas.dag import DAGSpec

NodeStatus = Literal[
    "planned",
    "ready",
    "running",
    "completed",
    "failed",
    "skipped",
]

NodePayloadType = Literal["capability", "start", "map", "subgraph", "loop"]


class CapabilityNodePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["capability"]
    invocation: CapabilityInvocation


class StartNodePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["start"]


class MapNodePayload(BaseModel):
    """Fan one capability invocation out over a runtime-resolved list."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["map"]
    items: ValueBinding
    invocation: CapabilityInvocation
    max_items: int = Field(default=64, ge=1)
    max_concurrency: int = Field(default=8, ge=1)


class SubgraphNodePayload(BaseModel):
    """Run an embedded DAGSpec; the node value is the subgraph's declared output."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["subgraph"]
    spec: "DAGSpec"
    input: Any = None


class LoopNodePayload(BaseModel):
    """Run an embedded DAGSpec repeatedly until ``until`` is truthy.

    Each iteration's output feeds the next iteration's graph input;
    ``until`` is evaluated against the latest output via map_item expressions.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["loop"]
    body: "DAGSpec"
    until: ValueBinding
    max_iterations: int = Field(ge=1)
    input: Any = None


NodePayload = Annotated[
    CapabilityNodePayload | StartNodePayload | MapNodePayload | SubgraphNodePayload | LoopNodePayload,
    Field(discriminator="type"),
]


class DAGNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = ""
    payload: NodePayload
    status: NodeStatus = "planned"
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
