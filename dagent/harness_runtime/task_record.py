"""Runtime task state for DAG-backed messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dagent.harness_runtime.dag_executor import RunResult
from dagent.harness_runtime.review_policy import ReviewLevel, ReviewKind
from dagent.schemas import DAG, NodeExecutionRecord


@dataclass
class PendingReview:
    review_id: str
    kind: ReviewKind
    message: str
    proposed_dag: DAG
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskRecord:
    task_id: str
    user_request: str
    dag: DAG
    runs: list[RunResult] = field(default_factory=list)
    pending_review: PendingReview | None = None
    review_level: ReviewLevel = "fast"
    runtime_mode: str = "auto"
    continuation_count: int = 0
    final_response: str = ""
    node_results: dict = field(default_factory=dict)
    trace_records: list[NodeExecutionRecord] = field(default_factory=list)
    dag_messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def node_execution_records(self) -> list[NodeExecutionRecord]:
        return self.trace_records
