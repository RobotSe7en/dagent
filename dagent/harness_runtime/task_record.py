"""Runtime task state for tool and DAG-backed messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from dagent.harness_runtime.review_policy import ReviewLevel, ReviewKind
from dagent.schemas import DAG, DAGNode, Boundary, ToolExecutionRecord, ToolInvocation
from dagent.schemas.trace import ToolExecutionSource, ToolExecutionStatus

if TYPE_CHECKING:
    from dagent.harness_runtime.dag_executor import DAGRunResult
    from dagent.harness_runtime.loop_outcome import LoopOutcome


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
    pending_invocation: ToolInvocation | None = None


@dataclass
class ToolTaskState:
    messages: list[dict[str, Any]] = field(default_factory=list)
    boundary: Boundary = field(default_factory=Boundary)
    steps: int = 0


@dataclass
class DAGTaskState:
    dag: DAG
    runtime_mode: str = "auto"
    runs: list[DAGRunResult] = field(default_factory=list)
    continuation_count: int = 0
    node_results: dict = field(default_factory=dict)
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
    execution_records: list[ToolExecutionRecord] = field(default_factory=list)
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
    def runs(self) -> list[DAGRunResult]:
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
    def dag_messages(self) -> list[dict[str, Any]]:
        return self.require_dag_state().dag_messages

    def apply_outcome(
        self,
        loop_outcome: "LoopOutcome",
        *,
        review_level: ReviewLevel,
        invocations: list[ToolInvocation] | None = None,
    ) -> None:
        task_invocations = invocations if invocations is not None else loop_outcome.invocations
        self.status = loop_outcome.status
        self.review_level = review_level
        self.pending_review = loop_outcome.pending_review
        self.final_response = loop_outcome.final_answer
        self.invocations = {
            invocation.invocation_id: invocation
            for invocation in task_invocations
        }

        if loop_outcome.dag is not None:
            if self.dag_state is None:
                self.dag_state = DAGTaskState(dag=loop_outcome.dag)
            self.dag_state.dag = loop_outcome.dag
            if loop_outcome.dag_run is not None:
                if loop_outcome.dag_run not in self.dag_state.runs:
                    self.dag_state.runs.append(loop_outcome.dag_run)
                self.dag_state.node_results = loop_outcome.dag_run.node_results
                self.execution_records = list(loop_outcome.dag_run.execution_records)
            return

        if self.mode == "tool":
            previous_boundary = (
                self.tool_state.boundary
                if self.tool_state is not None
                else Boundary(mode="read_only", allowed_paths=["."])
            )
            self.tool_state = ToolTaskState(
                messages=loop_outcome.messages,
                boundary=previous_boundary,
                steps=len(task_invocations),
            )
            self.execution_records = tool_loop_execution_records(
                task_id=self.task_id,
                messages=loop_outcome.messages,
                invocations=task_invocations,
            )


class ToolExecutionStore:
    """Stores raw tool execution records for planning and audit."""

    def __init__(self) -> None:
        self._records: list[ToolExecutionRecord] = []

    def add_record(
        self,
        *,
        task_id: str,
        invocation: ToolInvocation,
        source: ToolExecutionSource,
        output: str = "",
        error: str | None = None,
        status: ToolExecutionStatus,
        stop_reason: str,
        steps: int,
        dag: DAG | None = None,
        node: DAGNode | None = None,
    ) -> ToolExecutionRecord:
        record = ToolExecutionRecord(
            record_id=f"tool_execution_{uuid4().hex}",
            task_id=task_id,
            invocation=invocation.model_copy(deep=True),
            source=source,
            output=output,
            error=error,
            status=status,
            stop_reason=stop_reason,
            steps=steps,
            dag_id=dag.dag_id if dag else None,
            dag_version=dag.version if dag else None,
            node_id=node.id if node else None,
        )
        self._records.append(record)
        return record

    def records_for_task(self, task_id: str) -> list[ToolExecutionRecord]:
        return [record for record in self._records if record.task_id == task_id]

    def records_for_dag(self, dag_id: str) -> list[ToolExecutionRecord]:
        return [record for record in self._records if record.dag_id == dag_id]

    def records_for_node(self, task_id: str, node_id: str) -> list[ToolExecutionRecord]:
        return [
            record
            for record in self.records_for_task(task_id)
            if record.node_id == node_id
        ]

    def all_records(self) -> list[ToolExecutionRecord]:
        return list(self._records)


def task_context_payload(record: RuntimeTaskRecord) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "dag_id": record.dag.dag_id,
        "user_request": record.user_request,
        "dag_status": record.dag.status,
        "node_results": {
            node_id: {
                "completed": node_result.completed,
                "stop_reason": node_result.stop_reason,
                "final_response": node_result.final_response,
            }
            for node_id, node_result in record.node_results.items()
        },
        "recent_execution_records": [
            {
                "node_id": record_item.node_id,
                "tool": record_item.invocation.tool_name,
                "status": record_item.status,
                "output": record_item.output,
                "error": record_item.error,
            }
            for record_item in record.execution_records[-8:]
        ],
    }


def pending_review_invocation(loop_outcome: "LoopOutcome") -> ToolInvocation | None:
    review = loop_outcome.pending_review
    if review is None or review.kind != "tool_review":
        return None
    tool_call_id = (review.tool_call or {}).get("tool_call_id")
    if tool_call_id:
        for invocation in reversed(loop_outcome.invocations):
            if invocation.invocation_id == tool_call_id:
                return invocation
    return loop_outcome.invocations[-1] if loop_outcome.invocations else None


def tool_loop_execution_records(
    *,
    task_id: str,
    messages: list[dict[str, Any]],
    invocations: list[ToolInvocation],
) -> list[ToolExecutionRecord]:
    invocations_by_id = {
        invocation.invocation_id: invocation
        for invocation in invocations
    }
    tool_messages: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        tool_call_id = message.get("tool_call_id")
        if not isinstance(tool_call_id, str) or tool_call_id not in invocations_by_id:
            continue
        content = str(message.get("content", ""))
        if content.startswith("[PENDING_REVIEW]") or content.startswith("[DENIED]"):
            continue
        tool_messages[tool_call_id] = message

    records: list[ToolExecutionRecord] = []
    for tool_call_id, message in tool_messages.items():
        content = str(message.get("content", ""))
        failed = content.startswith(("[TOOL_ERROR]", "[BOUNDARY_VIOLATION]", "[ERROR]"))
        records.append(
            ToolExecutionRecord(
                record_id=f"tool_execution_{uuid4().hex}",
                task_id=task_id,
                invocation=invocations_by_id[tool_call_id].model_copy(deep=True),
                source="tool_loop",
                output="" if failed else content,
                error=content if failed else None,
                status="failed" if failed else "completed",
                stop_reason="tool_error" if failed else "completed",
                steps=1,
            )
        )
    return records
