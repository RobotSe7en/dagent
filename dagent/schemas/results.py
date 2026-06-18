"""Result and outcome schemas shared across dagent."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from dagent.schemas.dag import DAG
from dagent.schemas.capability import CapabilityInvocation
from dagent.schemas.run_trace import RunTrace
from dagent.schemas.sandbox import RunExecution


ReviewKind = Literal["initial_dag", "dag_replan", "capability_review"]
LoopStatus = Literal["completed", "awaiting_review", "failed"]
RunStateKind = Literal["tool", "dynamic_dag", "static_dag"]
ReviewLevelValue = Literal["fast", "careful"]
RuntimeModeValue = Literal["auto", "tool", "dag", "dag_spec"]


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


class RunState(BaseModel):
    """Serializable run state for display and cross-request resume."""

    run_id: str
    kind: RunStateKind
    status: LoopStatus
    internal_messages: list[dict[str, Any]] = Field(default_factory=list)
    input_message_count: int = 0
    dag: DAG | None = None
    trace: RunTrace | None = None
    pending_review: PendingReview | None = None
    pending_invocation: CapabilityInvocation | None = None
    user_request: str = ""
    review_level: ReviewLevelValue = "fast"
    runtime_mode: RuntimeModeValue = "auto"
    execution: RunExecution = "local"
    dynamic_adjust: bool = True
    capability_scope: RunCapabilityScope = Field(default_factory=RunCapabilityScope)
    spec_id: str | None = None
    workspace_path: str | None = None
    dag_boundary_approved_version: int | None = None


class LoopOutcome(BaseModel):
    """Common contract between loops and runtime orchestration."""

    state: RunState
    output_text: str = ""
    execution_context: str = ""


class ValidationIssue(BaseModel):
    message: str
    node_id: str | None = None


class ValidationResult(BaseModel):
    passed: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    summary: str = ""
