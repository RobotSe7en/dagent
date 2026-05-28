"""Runtime task state for tool and DAG-backed messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from dagent.review import ReviewLevel
from dagent.harness_runtime.capability_scope import CapabilityScope, DEFAULT_CAPABILITY_SCOPE
from dagent.schemas import (
    DAG,
    CapabilityInvocation,
    PendingReview,
    ReviewKind,
    RunTrace,
)

if TYPE_CHECKING:
    from dagent.schemas import LoopOutcome


RuntimeTaskMode = Literal["tool", "dag"]


@dataclass
class ReviewContinuation:
    review_id: str
    task_id: str
    kind: ReviewKind
    user_request: str
    review_level: ReviewLevel
    invocations: list[CapabilityInvocation] = field(default_factory=list)
    pending_invocation: CapabilityInvocation | None = None
    capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE


@dataclass
class RuntimeTaskRecord:
    task_id: str
    mode: RuntimeTaskMode
    user_request: str
    review_level: ReviewLevel = "fast"
    pending_review: PendingReview | None = None
    dag: DAG | None = None
    trace: RunTrace | None = None
    runtime_mode: str = "auto"
    spec_id: str | None = None
    workspace_path: str | None = None
    capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE

    @classmethod
    def dag_task(
        cls,
        *,
        task_id: str,
        user_request: str,
        dag: DAG,
        review_level: ReviewLevel = "fast",
        runtime_mode: str = "auto",
        spec_id: str | None = None,
        workspace_path: str | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
    ) -> "RuntimeTaskRecord":
        return cls(
            task_id=task_id,
            mode="dag",
            user_request=user_request,
            review_level=review_level,
            dag=dag,
            runtime_mode=runtime_mode,
            spec_id=spec_id,
            workspace_path=workspace_path,
            capability_scope=capability_scope,
        )

    @classmethod
    def tool_task(
        cls,
        *,
        task_id: str,
        user_request: str,
        review_level: ReviewLevel = "fast",
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
    ) -> "RuntimeTaskRecord":
        return cls(
            task_id=task_id,
            mode="tool",
            user_request=user_request,
            review_level=review_level,
            capability_scope=capability_scope,
        )

    def apply_outcome(
        self,
        loop_outcome: "LoopOutcome",
        *,
        review_level: ReviewLevel,
        capability_scope: CapabilityScope | None = None,
    ) -> None:
        self.review_level = review_level
        if capability_scope is not None:
            self.capability_scope = capability_scope
        self.pending_review = loop_outcome.pending_review
        if loop_outcome.dag is not None:
            self.dag = loop_outcome.dag
        if loop_outcome.trace is not None:
            self.trace = merge_run_traces(self.trace, loop_outcome.trace)
        if loop_outcome.spec_id is not None:
            self.spec_id = loop_outcome.spec_id
        if loop_outcome.workspace_path is not None:
            self.workspace_path = loop_outcome.workspace_path


def pending_review_invocation(
    loop_outcome: "LoopOutcome",
    invocations: list[CapabilityInvocation] | None = None,
) -> CapabilityInvocation | None:
    review = loop_outcome.pending_review
    if review is None or review.kind != "capability_review":
        return None
    task_invocations = invocations if invocations is not None else loop_outcome.invocations
    invocation_id = (review.capability_call or {}).get("invocation_id")
    if invocation_id:
        for invocation in reversed(task_invocations):
            if invocation.invocation_id == invocation_id:
                return invocation
    return task_invocations[-1] if task_invocations else None


def merge_run_traces(current: RunTrace | None, incoming: RunTrace) -> RunTrace:
    if current is None:
        return incoming
    merged = current.model_copy(deep=True)
    seen_ids = {child.id for child in merged.root.children}
    for child in incoming.root.children:
        if child.id in seen_ids:
            continue
        copied = child.model_copy(deep=True)
        copied.parent_id = merged.root.id
        _reparent_trace_children(copied)
        merged.root.children.append(copied)
        seen_ids.add(copied.id)
    merged.root.status = incoming.root.status
    merged.root.output = incoming.root.output if incoming.root.output is not None else merged.root.output
    merged.root.error = incoming.root.error
    merged.root.ended_at = incoming.root.ended_at
    merged.artifacts.update(incoming.artifacts)
    return merged


def _reparent_trace_children(parent) -> None:
    for child in parent.children:
        child.parent_id = parent.id
        _reparent_trace_children(child)
