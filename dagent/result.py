"""Public run result facade for the dagent SDK."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import field
from typing import Any, Literal

from pydantic import ConfigDict, Field as PydanticField, TypeAdapter
from pydantic.dataclasses import dataclass

from dagent.review import ReviewHandle
from dagent.schemas import (
    ArtifactState,
    ContextUsage,
    ConversationItem,
    ConversationState,
    DAG,
    DAGRun,
    PendingReview,
    ExecutionUsage,
    ResolvedRunPlan,
    ReviewKind,
    RunCheckpoint,
    RunState,
    RunTrace,
    RunTraceNode,
    ValidationIssue,
)


RunResultKind = Literal["tool", "dynamic_dag", "static_dag"]
RunStreamEventType = Literal[
    "run.started",
    "response.started",
    "response.reasoning.delta",
    "response.content.delta",
    "response.finished",
    "capability.call.started",
    "capability.call.completed",
    "capability.call.failed",
    "context.compaction.started",
    "context.compaction.finished",
    "dag.updated",
    "trace.updated",
    "validation.started",
    "validation.passed",
    "validation.retry",
    "review.required",
    "run.finished",
    "run.failed",
]

_STRICT = ConfigDict(extra="forbid")


@dataclass(frozen=True, config=_STRICT)
class RunResult:
    """Stable public SDK result for agent and DAG runs."""

    state: RunState
    output_text: str = ""
    new_items: tuple[ConversationItem, ...] = ()
    plan: ResolvedRunPlan | None = PydanticField(default=None, exclude=True)
    usage: ExecutionUsage = PydanticField(
        default_factory=ExecutionUsage,
    )
    output_value: Any = None

    @classmethod
    def model_validate(cls, value: Any) -> "RunResult":
        """Restore the serialized result projection; checkpoints are separate."""
        if isinstance(value, cls):
            return value
        return _RESULT_ADAPTER.validate_python(value)

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
    def conversation(self) -> ConversationState | None:
        return self.state.conversation

    @property
    def context_usage(self) -> tuple[ContextUsage, ...]:
        return tuple(self.state.context_usage)

    @property
    def checkpoint(self) -> RunCheckpoint | None:
        """Portable checkpoint for SDK-produced results."""

        if self.plan is None:
            return None
        return RunCheckpoint(
            schema_version=self.plan.schema_version,
            state=self.state,
            plan=self.plan,
            usage=self.usage,
        )

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
        node = _node_trace(self.trace, node_id)
        return node.value if node.value is not None else node.output

    def model_dump(self, *, mode: Literal["python", "json"] = "python") -> dict[str, Any]:
        return _RESULT_ADAPTER.dump_python(self, mode=mode)


_RESULT_ADAPTER = TypeAdapter(RunResult)


@dataclass(frozen=True, config=_STRICT)
class RunStartedData:
    """First stream event of every run; the envelope ``run_id`` is already final."""

    kind: RunResultKind


@dataclass(frozen=True, config=_STRICT)
class ResponseStartedData:
    """Marks the start of one model call; ``response_id`` keys its deltas."""

    response_id: str
    model_step: int | None = None
    run_id: str | None = None
    dag_id: str | None = None
    node_id: str | None = None
    parent_capability_id: str | None = None


@dataclass(frozen=True, config=_STRICT)
class TextDeltaData:
    delta: str
    response_id: str
    model_step: int | None = None
    run_id: str | None = None
    dag_id: str | None = None
    node_id: str | None = None
    parent_capability_id: str | None = None


@dataclass(frozen=True, config=_STRICT)
class ResponseFinishedData:
    """Marks the end of the model call identified by ``response_id``."""

    response_id: str
    model_step: int | None = None
    run_id: str | None = None
    dag_id: str | None = None
    node_id: str | None = None
    parent_capability_id: str | None = None


@dataclass(frozen=True, config=_STRICT)
class DagUpdatedData:
    dag: DAG


@dataclass(frozen=True, config=_STRICT)
class TraceUpdatedData:
    trace: RunTrace


@dataclass(frozen=True, config=_STRICT)
class ReviewRequiredData:
    """Signals that a run is awaiting review; full review data lives in ``run.finished`` state."""

    review_id: str
    kind: ReviewKind
    message: str


@dataclass(frozen=True, config=_STRICT)
class ValidationStartedData:
    message: str


@dataclass(frozen=True, config=_STRICT)
class ValidationPassedData:
    summary: str = ""
    issues: list[ValidationIssue] = field(default_factory=list)


@dataclass(frozen=True, config=_STRICT)
class ValidationRetryData:
    summary: str = ""
    issues: list[ValidationIssue] = field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True, config=_STRICT)
class CapabilityCallStartedData:
    invocation_id: str
    capability_id: str
    arguments: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    dag_id: str | None = None
    node_id: str | None = None
    parent_capability_id: str | None = None


@dataclass(frozen=True, config=_STRICT)
class CapabilityCallCompletedData:
    invocation_id: str
    capability_id: str
    content: str = ""
    run_id: str | None = None
    dag_id: str | None = None
    node_id: str | None = None
    parent_capability_id: str | None = None


@dataclass(frozen=True, config=_STRICT)
class CapabilityCallFailedData:
    invocation_id: str
    capability_id: str
    content: str = ""
    run_id: str | None = None
    dag_id: str | None = None
    node_id: str | None = None
    parent_capability_id: str | None = None


@dataclass(frozen=True, config=_STRICT)
class ContextCompactionStartedData:
    item_count: int
    scope: str = "conversation"


@dataclass(frozen=True, config=_STRICT)
class ContextCompactionFinishedData:
    usage: ContextUsage
    scope: str = "conversation"


@dataclass(frozen=True, config=_STRICT)
class RunFinishedData:
    result: RunResult


@dataclass(frozen=True, config=_STRICT)
class RunFailedData:
    message: str
    error_type: str


RunStreamEventData = (
    RunStartedData
    | ResponseStartedData
    | TextDeltaData
    | ResponseFinishedData
    | DagUpdatedData
    | TraceUpdatedData
    | ReviewRequiredData
    | ValidationStartedData
    | ValidationPassedData
    | ValidationRetryData
    | CapabilityCallStartedData
    | CapabilityCallCompletedData
    | CapabilityCallFailedData
    | ContextCompactionStartedData
    | ContextCompactionFinishedData
    | RunFinishedData
    | RunFailedData
)


@dataclass(frozen=True, config=_STRICT)
class RunStreamEvent:
    """Typed runtime event yielded by streams or observed during DAG design."""

    type: RunStreamEventType
    data: RunStreamEventData
    sequence: int = 0
    run_id: str | None = None

    @classmethod
    def model_validate(cls, value: Any) -> "RunStreamEvent":
        """Restore a serialized stream event."""
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return _EVENT_ADAPTER.validate_python(value)
        event_type = value.get("type")
        data_adapter = (
            _EVENT_DATA_ADAPTERS.get(event_type)
            if isinstance(event_type, str)
            else None
        )
        if data_adapter is None:
            return _EVENT_ADAPTER.validate_python(value)
        payload = dict(value)
        payload["data"] = data_adapter.validate_python(value.get("data"))
        return _EVENT_ADAPTER.validate_python(payload)

    def model_dump(self, *, mode: Literal["python", "json"] = "python") -> dict[str, Any]:
        return _EVENT_ADAPTER.dump_python(self, mode=mode)


_EVENT_ADAPTER = TypeAdapter(RunStreamEvent)
_EVENT_DATA_ADAPTERS: dict[str, TypeAdapter[Any]] = {
    "run.started": TypeAdapter(RunStartedData),
    "response.started": TypeAdapter(ResponseStartedData),
    "response.reasoning.delta": TypeAdapter(TextDeltaData),
    "response.content.delta": TypeAdapter(TextDeltaData),
    "response.finished": TypeAdapter(ResponseFinishedData),
    "capability.call.started": TypeAdapter(CapabilityCallStartedData),
    "capability.call.completed": TypeAdapter(CapabilityCallCompletedData),
    "capability.call.failed": TypeAdapter(CapabilityCallFailedData),
    "context.compaction.started": TypeAdapter(ContextCompactionStartedData),
    "context.compaction.finished": TypeAdapter(ContextCompactionFinishedData),
    "dag.updated": TypeAdapter(DagUpdatedData),
    "trace.updated": TypeAdapter(TraceUpdatedData),
    "validation.started": TypeAdapter(ValidationStartedData),
    "validation.passed": TypeAdapter(ValidationPassedData),
    "validation.retry": TypeAdapter(ValidationRetryData),
    "review.required": TypeAdapter(ReviewRequiredData),
    "run.finished": TypeAdapter(RunFinishedData),
    "run.failed": TypeAdapter(RunFailedData),
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
