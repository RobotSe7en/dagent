"""Result and outcome schemas shared across dagent."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from dagent.schemas.dag import DAG
from dagent.schemas.capability import CapabilityInvocation
from dagent.schemas.run_trace import RunTrace


ReviewKind = Literal["initial_dag", "dag_replan", "capability_review"]
LoopStatus = Literal["completed", "awaiting_review", "failed"]


class PendingReview(BaseModel):
    review_id: str
    kind: ReviewKind
    message: str
    proposed_dag: DAG | None = None
    capability_call: dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


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
