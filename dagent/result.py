"""Public run result facade for the dagent SDK."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Literal

from dagent.review import ReviewHandle
from dagent.schemas import (
    ArtifactState,
    DAG,
    DAGRun,
    PendingReview,
    ReviewKind,
    RunState,
    RunTrace,
    RunTraceNode,
    ValidationIssue,
)


RunResultKind = Literal["tool", "dynamic_dag", "static_dag"]
RunStreamEventType = Literal[
    "run.status",
    "run.finished",
    "run.failed",
    "response.raw.delta",
    "response.reasoning.delta",
    "response.content.delta",
    "dag.updated",
    "trace.updated",
    "review.required",
    "validation.started",
    "validation.passed",
    "validation.retry",
    "capability.call.started",
    "capability.call.completed",
    "capability.call.failed",
]


@dataclass(frozen=True)
class RunResult:
    """Stable public SDK result for agent and DAG runs."""

    state: RunState
    output_text: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def kind(self) -> RunResultKind:
        return self.state.kind

    @property
    def status(self) -> str:
        return self.state.status

    @property
    def run_id(self) -> str:
        return self.state.run_id

    @property
    def pending_review(self) -> PendingReview | None:
        return self.state.pending_review

    @property
    def dag_run(self) -> DAGRun | None:
        if self.kind != "static_dag":
            return None
        if self.state.dag is None or self.state.trace is None:
            return None
        return DAGRun(
            run_id=self.run_id,
            spec_id=self.state.spec_id,
            workspace_path=self.state.workspace_path or "",
            dag=self.state.dag,
            trace=self.state.trace,
        )

    @property
    def dag(self) -> DAG | None:
        return self.state.dag

    @property
    def trace(self) -> RunTrace | None:
        return self.state.trace

    @property
    def spec_id(self) -> str | None:
        return self.state.spec_id

    @property
    def workspace_path(self) -> str | None:
        return self.state.workspace_path

    @property
    def requires_review(self) -> bool:
        return self.status == "awaiting_review" and self.pending_review is not None

    @property
    def review(self) -> ReviewHandle | None:
        if self.pending_review is None:
            return None
        return ReviewHandle(self.pending_review)

    @property
    def artifacts(self) -> dict[str, ArtifactState]:
        if self.trace is None:
            return {}
        return self.trace.artifacts

    def artifact_state(self, artifact_id: str) -> ArtifactState:
        try:
            return self.artifacts[artifact_id]
        except KeyError as exc:
            raise KeyError(f"Artifact '{artifact_id}' was not found in this run.") from exc

    def node_output(self, node_id: str) -> Any:
        return _node_trace(self.trace, node_id).output

    def node_value(self, node_id: str) -> Any:
        return _node_trace(self.trace, node_id).value

    def model_dump(self, *, mode: Literal["python", "json"] = "python") -> dict[str, Any]:
        return {
            "output_text": self.output_text,
            "messages": _dump(self.messages, mode=mode),
            "state": _dump(self.state, mode=mode),
        }


@dataclass(frozen=True)
class TextDeltaData:
    delta: str


@dataclass(frozen=True)
class StatusData:
    message: str


@dataclass(frozen=True)
class DagUpdatedData:
    dag: DAG


@dataclass(frozen=True)
class TraceUpdatedData:
    trace: RunTrace


@dataclass(frozen=True)
class ReviewRequiredData:
    review_id: str
    kind: ReviewKind
    message: str
    dag: DAG | None = None
    capability_call: dict[str, Any] | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_pending_review(self) -> PendingReview:
        return PendingReview(
            review_id=self.review_id,
            kind=self.kind,
            message=self.message,
            proposed_dag=self.dag,
            capability_call=self.capability_call,
            payload=dict(self.payload),
        )

    def to_handle(self) -> ReviewHandle:
        return ReviewHandle(self.to_pending_review())


@dataclass(frozen=True)
class ValidationStartedData:
    message: str


@dataclass(frozen=True)
class ValidationPassedData:
    summary: str = ""
    issues: list[ValidationIssue] = field(default_factory=list)


@dataclass(frozen=True)
class ValidationRetryData:
    summary: str = ""
    issues: list[ValidationIssue] = field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class CapabilityCallStartedData:
    invocation_id: str
    capability_id: str
    arguments: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    dag_id: str | None = None
    node_id: str | None = None
    parent_capability_id: str | None = None


@dataclass(frozen=True)
class CapabilityCallCompletedData:
    invocation_id: str
    capability_id: str
    content: str = ""
    run_id: str | None = None
    dag_id: str | None = None
    node_id: str | None = None
    parent_capability_id: str | None = None


@dataclass(frozen=True)
class CapabilityCallFailedData:
    invocation_id: str
    capability_id: str
    content: str = ""
    run_id: str | None = None
    dag_id: str | None = None
    node_id: str | None = None
    parent_capability_id: str | None = None


@dataclass(frozen=True)
class RunFinishedData:
    result: RunResult


@dataclass(frozen=True)
class RunFailedData:
    message: str
    error_type: str


RunStreamEventData = (
    TextDeltaData
    | StatusData
    | DagUpdatedData
    | TraceUpdatedData
    | ReviewRequiredData
    | ValidationStartedData
    | ValidationPassedData
    | ValidationRetryData
    | CapabilityCallStartedData
    | CapabilityCallCompletedData
    | CapabilityCallFailedData
    | RunFinishedData
    | RunFailedData
)


@dataclass(frozen=True)
class RunStreamEvent:
    """Low-level typed event yielded by ``Runner.stream_events``."""

    type: RunStreamEventType
    data: RunStreamEventData
    sequence: int = 0
    run_id: str | None = None

    def model_dump(self, *, mode: Literal["python", "json"] = "python") -> dict[str, Any]:
        return {
            "type": self.type,
            "data": _dump(self.data, mode=mode),
            "sequence": self.sequence,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class RunStreamChunk:
    """High-level stream item yielded by ``Runner.stream``."""

    text: str = ""
    review: ReviewHandle | None = None
    result: RunResult | None = None
    event: RunStreamEvent | None = None

    def model_dump(self, *, mode: Literal["python", "json"] = "python") -> dict[str, Any]:
        return {
            "text": self.text,
            "review": _dump(self.review.pending if self.review is not None else None, mode=mode),
            "result": self.result.model_dump(mode=mode) if self.result is not None else None,
            "event": self.event.model_dump(mode=mode) if self.event is not None else None,
        }


def _node_trace(trace: RunTrace | None, node_id: str) -> RunTraceNode:
    if trace is None:
        raise KeyError(f"Node '{node_id}' was not found because this run has no trace.")
    stack = list(trace.root.children)
    while stack:
        node = stack.pop()
        if node.kind == "dag_node" and node.ref.get("node_id") == node_id:
            return node
        stack.extend(node.children)
    raise KeyError(f"Node '{node_id}' was not found in this run trace.")


def _dump(value: Any, *, mode: Literal["python", "json"]) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode=mode)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _dump(getattr(value, item.name), mode=mode)
            for item in fields(value)
        }
    if isinstance(value, dict):
        return {key: _dump(item, mode=mode) for key, item in value.items()}
    if isinstance(value, list):
        return [_dump(item, mode=mode) for item in value]
    if isinstance(value, tuple):
        return [_dump(item, mode=mode) for item in value] if mode == "json" else tuple(
            _dump(item, mode=mode) for item in value
        )
    return value
