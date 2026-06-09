"""Result and outcome schemas shared across dagent."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from dagent.schemas.dag import DAG
from dagent.schemas.capability import CapabilityInvocation
from dagent.schemas.run_trace import RunTrace


ReviewKind = Literal["initial_dag", "dag_replan", "capability_review"]
LoopStatus = Literal["completed", "awaiting_review", "failed"]
RunStateKind = Literal["tool", "dynamic_dag", "static_dag"]
ReviewLevelValue = Literal["fast", "careful"]
RuntimeModeValue = Literal["auto", "tool", "dag", "dag_spec"]


class RunMessage(BaseModel):
    """OpenAI-compatible message stored by dagent run state."""

    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None


class RunCapabilityScope(BaseModel):
    """Serializable capability visibility for a resumable run."""

    capability_ids: tuple[str, ...] | None = None
    skills: tuple[str, ...] | None = None


class PendingReview(BaseModel):
    review_id: str
    kind: ReviewKind
    message: str
    proposed_dag: DAG | None = None
    capability_call: dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class RunReviewContinuation(BaseModel):
    """Serializable review continuation needed to resume an interrupted run."""

    review_id: str
    task_id: str
    kind: ReviewKind
    user_request: str
    review_level: ReviewLevelValue = "fast"
    invocations: list[CapabilityInvocation] = Field(default_factory=list)
    pending_invocation: CapabilityInvocation | None = None
    capability_scope: RunCapabilityScope = Field(default_factory=RunCapabilityScope)


class RunState(BaseModel):
    """Serializable SDK state for web display and cross-request resume."""

    run_id: str | None = None
    kind: RunStateKind
    status: LoopStatus
    internal_messages: list[dict[str, Any]] = Field(default_factory=list)
    dag: DAG | None = None
    trace: RunTrace | None = None
    pending_review: PendingReview | None = None
    review_continuation: RunReviewContinuation | None = None
    user_request: str = ""
    review_level: ReviewLevelValue = "fast"
    runtime_mode: RuntimeModeValue = "auto"
    capability_scope: RunCapabilityScope = Field(default_factory=RunCapabilityScope)
    spec_id: str | None = None
    workspace_path: str | None = None


class LoopOutcome(BaseModel):
    """Common contract between loops and runtime orchestration."""

    status: LoopStatus
    execution_context: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    final_answer: str = ""
    events: list[dict[str, Any]] = Field(default_factory=list)
    invocations: list[CapabilityInvocation] = Field(default_factory=list)
    dag: DAG | None = None
    trace: RunTrace | None = None
    task_id: str | None = None
    spec_id: str | None = None
    workspace_path: str | None = None
    pending_review: PendingReview | None = None


class RuntimeResponse(BaseModel):
    status: LoopStatus
    final_answer: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    state: RunState | None = None
    dag: DAG | None = None
    trace: RunTrace | None = None
    task_id: str | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    pending_review: PendingReview | None = None


class ValidationIssue(BaseModel):
    message: str
    node_id: str | None = None


class ValidationResult(BaseModel):
    passed: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    summary: str = ""
