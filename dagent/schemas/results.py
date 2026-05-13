"""Result and outcome schemas shared across dagent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from dagent.schemas.dag import DAG
from dagent.schemas.invocation import ToolInvocation
from dagent.schemas.trace import ToolExecutionRecord, TraceEvent


ReviewKind = Literal["initial_dag", "dag_replan", "tool_review"]
LoopStatus = Literal["completed", "awaiting_review", "failed"]


@dataclass(frozen=True)
class DAGNodeResult:
    node_id: str
    final_response: str
    completed: bool
    stop_reason: str
    steps: int


@dataclass(frozen=True)
class DAGRunResult:
    dag_id: str
    completed: bool
    node_results: dict[str, DAGNodeResult]
    traces: list[TraceEvent] = field(default_factory=list)
    execution_records: list[ToolExecutionRecord] = field(default_factory=list)


@dataclass
class PendingReview:
    review_id: str
    kind: ReviewKind
    message: str
    proposed_dag: DAG | None = None
    tool_call: dict[str, Any] | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoopOutcome:
    """Common contract between loops and runtime orchestration."""

    status: LoopStatus
    execution_context: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    final_answer: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    invocations: list[ToolInvocation] = field(default_factory=list)
    dag: DAG | None = None
    dag_run: DAGRunResult | None = None
    task_id: str | None = None
    pending_review: PendingReview | None = None


@dataclass(frozen=True)
class RuntimeResponse:
    status: LoopStatus
    final_answer: str
    dag: DAG | None = None
    dag_run: DAGRunResult | None = None
    task_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    pending_review: PendingReview | None = None


@dataclass(frozen=True)
class ValidationIssue:
    message: str
    node_id: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    summary: str = ""
