"""Unified run trace and result tree schemas."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from dagent.schemas.artifact import ArtifactState
from dagent.schemas.capability import CapabilityInvocation, CapabilityResult


RunTraceStatus = Literal[
    "planned",
    "running",
    "awaiting_review",
    "completed",
    "failed",
    "skipped",
    "cancelled",
]

RunTraceNodeKind = Literal[
    "run",
    "dag_node",
    "agent_loop",
    "agent_step",
    "model_call",
    "capability_call",
    "review",
    "artifact",
]


class RunTraceError(BaseModel):
    message: str
    code: str = ""


class CapabilityExecution(BaseModel):
    invocation: CapabilityInvocation
    result: CapabilityResult | None = None


class RunTraceNode(BaseModel):
    id: str = Field(default_factory=lambda: f"trace_node_{uuid4().hex}")
    parent_id: str | None = None
    kind: RunTraceNodeKind
    status: RunTraceStatus = "running"
    label: str = ""
    started_at: datetime | None = Field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    step_count: int = 0
    ref: dict[str, str] = Field(default_factory=dict)
    input: dict[str, Any] = Field(default_factory=dict)
    output: Any | None = None
    value: Any | None = None
    error: RunTraceError | None = None
    capability_execution: CapabilityExecution | None = None
    children: list["RunTraceNode"] = Field(default_factory=list)

    @classmethod
    def run(cls, *, run_id: str, status: RunTraceStatus = "running") -> "RunTraceNode":
        return cls(
            id=f"run_trace_node_{run_id}",
            kind="run",
            status=status,
            ref={"run_id": run_id},
            label=run_id,
        )

    @classmethod
    def dag_node(
        cls,
        *,
        parent_id: str,
        node_id: str,
        status: RunTraceStatus = "running",
        label: str = "",
    ) -> "RunTraceNode":
        return cls(
            parent_id=parent_id,
            kind="dag_node",
            status=status,
            ref={"node_id": node_id},
            label=label or node_id,
        )

    def upsert_child(self, child: "RunTraceNode") -> None:
        """Replace a same-kind/ref child in place, or append it."""
        for index, existing in enumerate(self.children):
            if existing.kind == child.kind and existing.ref == child.ref:
                self.children[index] = child
                return
        self.children.append(child)

    def reparent_children(self) -> None:
        """Rewrite each descendant's parent_id to match its actual parent."""
        for child in self.children:
            child.parent_id = self.id
            child.reparent_children()

    @classmethod
    def capability_call(
        cls,
        *,
        parent_id: str,
        invocation: CapabilityInvocation,
        result: CapabilityResult | None = None,
        status: RunTraceStatus | None = None,
        output: Any | None = None,
        error: str | None = None,
    ) -> "RunTraceNode":
        resolved_status: RunTraceStatus
        if status is not None:
            resolved_status = status
        elif result is None:
            resolved_status = "running"
        else:
            resolved_status = "completed" if result.status == "completed" else "failed"
        return cls(
            parent_id=parent_id,
            kind="capability_call",
            status=resolved_status,
            label=invocation.capability_id,
            ref={
                "invocation_id": invocation.invocation_id,
                "capability_id": invocation.capability_id,
            },
            input=invocation.arguments,
            output=output,
            value=result.value if result is not None else None,
            error=RunTraceError(message=error) if error else None,
            capability_execution=CapabilityExecution(invocation=invocation, result=result),
            ended_at=datetime.now(timezone.utc) if result is not None or error else None,
        )


class RunTrace(BaseModel):
    run_id: str
    root: RunTraceNode
    artifacts: dict[str, ArtifactState] = Field(default_factory=dict)

    @property
    def status(self) -> RunTraceStatus:
        return self.root.status

    def dag_node_traces(self) -> dict[str, RunTraceNode]:
        """Map ``node_id`` to its DAG-node child trace under the run root."""
        return {
            node.ref["node_id"]: node
            for node in self.root.children
            if node.kind == "dag_node" and node.ref.get("node_id")
        }

    def merge(self, incoming: "RunTrace") -> "RunTrace":
        """Return a copy of this trace with ``incoming``'s new children folded in."""
        merged = self.model_copy(deep=True)
        seen_ids = {child.id for child in merged.root.children}
        for child in incoming.root.children:
            if child.id in seen_ids:
                continue
            copied = child.model_copy(deep=True)
            copied.parent_id = merged.root.id
            copied.reparent_children()
            merged.root.children.append(copied)
            seen_ids.add(copied.id)
        merged.root.status = incoming.root.status
        if incoming.root.output is not None:
            merged.root.output = incoming.root.output
        merged.root.error = incoming.root.error
        merged.root.ended_at = incoming.root.ended_at
        merged.artifacts.update(incoming.artifacts)
        return merged
