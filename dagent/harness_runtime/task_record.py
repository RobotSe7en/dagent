"""Runtime task state for tool and DAG-backed messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from dagent.review import ReviewLevel
from dagent.harness_runtime.capability_scope import CapabilityScope, DEFAULT_CAPABILITY_SCOPE
from dagent.schemas import (
    DAG,
    CapabilityInvocation,
    PendingReview,
    RunCapabilityScope,
    RunReviewContinuation,
    RunState,
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
    messages: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_run_state(cls, state: RunReviewContinuation) -> "ReviewContinuation":
        return cls(
            review_id=state.review_id,
            task_id=state.task_id,
            kind=state.kind,
            user_request=state.user_request,
            review_level=state.review_level,
            invocations=list(state.invocations),
            pending_invocation=state.pending_invocation,
            capability_scope=capability_scope_from_state(state.capability_scope),
        )

    def to_run_state(self) -> RunReviewContinuation:
        return RunReviewContinuation(
            review_id=self.review_id,
            task_id=self.task_id,
            kind=self.kind,
            user_request=self.user_request,
            review_level=self.review_level,
            invocations=list(self.invocations),
            pending_invocation=self.pending_invocation,
            capability_scope=capability_scope_to_state(self.capability_scope),
        )


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
    internal_messages: list[dict[str, Any]] = field(default_factory=list)

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
            self.trace = (
                loop_outcome.trace
                if self.trace is None
                else self.trace.merge(loop_outcome.trace)
            )
        if loop_outcome.spec_id is not None:
            self.spec_id = loop_outcome.spec_id
        if loop_outcome.workspace_path is not None:
            self.workspace_path = loop_outcome.workspace_path
        self.internal_messages = list(loop_outcome.messages)

    @classmethod
    def from_run_state(cls, state: RunState) -> "RuntimeTaskRecord":
        mode: RuntimeTaskMode = "tool" if state.kind == "tool" else "dag"
        return cls(
            task_id=state.run_id or "",
            mode=mode,
            user_request=state.user_request,
            review_level=state.review_level,
            pending_review=state.pending_review,
            dag=state.dag,
            trace=state.trace,
            runtime_mode=state.runtime_mode,
            spec_id=state.spec_id,
            workspace_path=state.workspace_path,
            capability_scope=capability_scope_from_state(state.capability_scope),
            internal_messages=list(state.internal_messages),
        )

    def to_run_state(
        self,
        *,
        kind: Literal["tool", "dynamic_dag", "static_dag"],
        status: Literal["completed", "awaiting_review", "failed"],
        review_continuation: "ReviewContinuation | None" = None,
    ) -> RunState:
        return RunState(
            run_id=self.task_id,
            kind=kind,
            status=status,
            internal_messages=list(self.internal_messages),
            dag=self.dag,
            trace=self.trace,
            pending_review=self.pending_review,
            review_continuation=(
                review_continuation.to_run_state()
                if review_continuation is not None
                else None
            ),
            user_request=self.user_request,
            review_level=self.review_level,
            runtime_mode=self.runtime_mode,  # type: ignore[arg-type]
            capability_scope=capability_scope_to_state(self.capability_scope),
            spec_id=self.spec_id,
            workspace_path=self.workspace_path,
        )


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


def capability_scope_to_state(scope: CapabilityScope) -> RunCapabilityScope:
    return RunCapabilityScope(
        capability_ids=scope.capability_ids,
        skills=scope.skills,
    )


def capability_scope_from_state(scope: RunCapabilityScope) -> CapabilityScope:
    return CapabilityScope(
        capability_ids=scope.capability_ids,
        skills=scope.skills,
    )
