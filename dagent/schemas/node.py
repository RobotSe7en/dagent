"""DAG node schemas."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from dagent.schemas.capability import CapabilityInvocation

NodeStatus = Literal[
    "planned",
    "ready",
    "running",
    "completed",
    "failed",
    "skipped",
]

NodePayloadType = Literal["capability", "start"]


class CapabilityNodePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["capability"]
    invocation: CapabilityInvocation


class StartNodePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["start"]


NodePayload = Annotated[
    CapabilityNodePayload | StartNodePayload,
    Field(discriminator="type"),
]


class DAGNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = ""
    goal: str | None = None
    instructions: str | None = None
    payload: NodePayload
    status: NodeStatus = "planned"
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)

