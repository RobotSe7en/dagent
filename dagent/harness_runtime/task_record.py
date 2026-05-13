"""Runtime task state for tool and DAG-backed messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from dagent.harness_runtime.dag_executor import RunResult
from dagent.harness_runtime.review_policy import ReviewLevel, ReviewKind
from dagent.schemas import DAG, Boundary, NodeExecutionRecord, ToolInvocation


RuntimeTaskMode = Literal["tool", "dag"]
RuntimeTaskStatus = Literal["running", "awaiting_review", "completed", "failed"]


@dataclass
class PendingReview:
    review_id: str
    kind: ReviewKind
    message: str
    proposed_dag: DAG | None = None
    tool_call: dict[str, Any] | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewContinuation:
    review_id: str
    task_id: str
    kind: ReviewKind
    user_request: str
    review_level: ReviewLevel
    messages: list[dict[str, Any]] = field(default_factory=list)
    invocations: list[ToolInvocation] = field(default_factory=list)
    boundary: Boundary = field(default_factory=Boundary)
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    risk: str = "low"


@dataclass
class ToolTaskState:
    messages: list[dict[str, Any]] = field(default_factory=list)
    boundary: Boundary = field(default_factory=Boundary)
    steps: int = 0


@dataclass
class DAGTaskState:
    dag: DAG
    runtime_mode: str = "auto"
    runs: list[RunResult] = field(default_factory=list)
    continuation_count: int = 0
    node_results: dict = field(default_factory=dict)
    trace_records: list[NodeExecutionRecord] = field(default_factory=list)
    dag_messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RuntimeTaskRecord:
    task_id: str
    mode: RuntimeTaskMode
    user_request: str
    status: RuntimeTaskStatus = "running"
    review_level: ReviewLevel = "fast"
    pending_review: PendingReview | None = None
    final_response: str = ""
    invocations: dict[str, ToolInvocation] = field(default_factory=dict)
    execution_records: list[NodeExecutionRecord] = field(default_factory=list)
    tool_state: ToolTaskState | None = None
    dag_state: DAGTaskState | None = None

    @classmethod
    def dag_task(
        cls,
        *,
        task_id: str,
        user_request: str,
        dag: DAG,
        review_level: ReviewLevel = "fast",
        runtime_mode: str = "auto",
        dag_messages: list[dict[str, Any]] | None = None,
    ) -> "RuntimeTaskRecord":
        return cls(
            task_id=task_id,
            mode="dag",
            user_request=user_request,
            review_level=review_level,
            dag_state=DAGTaskState(
                dag=dag,
                runtime_mode=runtime_mode,
                dag_messages=list(dag_messages or []),
            ),
        )

    @classmethod
    def tool_task(
        cls,
        *,
        task_id: str,
        user_request: str,
        review_level: ReviewLevel = "fast",
    ) -> "RuntimeTaskRecord":
        return cls(
            task_id=task_id,
            mode="tool",
            user_request=user_request,
            review_level=review_level,
            tool_state=ToolTaskState(),
        )

    def require_dag_state(self) -> DAGTaskState:
        if self.dag_state is None:
            raise RuntimeError(f"Task '{self.task_id}' is not a DAG task.")
        return self.dag_state

    @property
    def dag(self) -> DAG:
        return self.require_dag_state().dag

    @dag.setter
    def dag(self, value: DAG) -> None:
        self.require_dag_state().dag = value

    @property
    def runs(self) -> list[RunResult]:
        return self.require_dag_state().runs

    @property
    def runtime_mode(self) -> str:
        return self.require_dag_state().runtime_mode

    @property
    def continuation_count(self) -> int:
        return self.require_dag_state().continuation_count

    @continuation_count.setter
    def continuation_count(self, value: int) -> None:
        self.require_dag_state().continuation_count = value

    @property
    def node_results(self) -> dict:
        return self.require_dag_state().node_results

    @node_results.setter
    def node_results(self, value: dict) -> None:
        self.require_dag_state().node_results = value

    @property
    def trace_records(self) -> list[NodeExecutionRecord]:
        return self.require_dag_state().trace_records

    @trace_records.setter
    def trace_records(self, value: list[NodeExecutionRecord]) -> None:
        self.require_dag_state().trace_records = value

    @property
    def dag_messages(self) -> list[dict[str, Any]]:
        return self.require_dag_state().dag_messages

    @property
    def node_execution_records(self) -> list[NodeExecutionRecord]:
        return self.trace_records
