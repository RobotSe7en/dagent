"""Runtime task state for DAG-backed messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dagent.harness_runtime.dag_executor import RunResult
from dagent.harness_runtime.dag_replanner import ReplanDecision
from dagent.harness_runtime.review_policy import ReviewLevel, ReviewKind
from dagent.schemas import DAG, NodeExecutionRecord, PermissionRequest


@dataclass
class PendingReview:
    review_id: str
    kind: ReviewKind
    message: str
    proposed_dag: DAG
    decision: ReplanDecision | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskRecord:
    task_id: str
    user_request: str
    dag: DAG
    runs: list[RunResult] = field(default_factory=list)
    pending_permission_request: PermissionRequest | None = None
    pending_review: PendingReview | None = None
    review_level: ReviewLevel = "balanced"
    suppress_next_review: bool = False
    node_results: dict = field(default_factory=dict)
    trace_records: list[NodeExecutionRecord] = field(default_factory=list)

    @property
    def node_execution_records(self) -> list[NodeExecutionRecord]:
        return self.trace_records
