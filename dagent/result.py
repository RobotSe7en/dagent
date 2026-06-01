"""Public run result facade for the dagent SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from dagent.review import ReviewHandle
from dagent.schemas import ArtifactState, DAG, DAGRun, PendingReview, RunTrace, RunTraceNode, RuntimeResponse


RunResultKind = Literal["tool", "dynamic_dag", "static_dag"]


@dataclass(frozen=True)
class RunResult:
    """Stable public wrapper around all runner execution results."""

    raw_response: RuntimeResponse | DAGRun
    kind: RunResultKind | None = None

    def __post_init__(self) -> None:
        if self.kind is None:
            object.__setattr__(self, "kind", _infer_kind(self.raw_response))

    @property
    def dag_run(self) -> DAGRun | None:
        return self.raw_response if isinstance(self.raw_response, DAGRun) else None

    @property
    def status(self) -> str:
        return self.raw_response.status

    @property
    def output_text(self) -> str:
        if isinstance(self.raw_response, RuntimeResponse):
            return self.raw_response.final_answer
        return _dag_run_output_text(self.raw_response)

    @property
    def dag(self) -> DAG | None:
        return self.raw_response.dag

    @property
    def trace(self) -> RunTrace | None:
        return self.raw_response.trace

    @property
    def run_id(self) -> str | None:
        if isinstance(self.raw_response, DAGRun):
            return self.raw_response.run_id
        return self.raw_response.task_id

    @property
    def spec_id(self) -> str | None:
        if isinstance(self.raw_response, DAGRun):
            return self.raw_response.spec_id
        return None

    @property
    def workspace_path(self) -> str | None:
        if isinstance(self.raw_response, DAGRun):
            return self.raw_response.workspace_path
        return None

    @property
    def events(self) -> list[dict[str, Any]]:
        if isinstance(self.raw_response, RuntimeResponse):
            return self.raw_response.events
        return []

    @property
    def pending_review(self) -> PendingReview | None:
        if isinstance(self.raw_response, RuntimeResponse):
            return self.raw_response.pending_review
        return None

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


@dataclass(frozen=True)
class RunStreamEvent:
    """Public event yielded by ``Runner.stream``."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    result: RunResult | None = None
    error: BaseException | None = None

    @property
    def content(self) -> str:
        return str(self.data.get("content", ""))


def _infer_kind(raw: RuntimeResponse | DAGRun) -> RunResultKind:
    if isinstance(raw, DAGRun):
        return "static_dag"
    if raw.dag is not None:
        return "dynamic_dag"
    return "tool"


def _dag_run_output_text(run: DAGRun) -> str:
    if run.trace.root.output:
        return str(run.trace.root.output)
    if run.status == "completed":
        return "DAG execution completed."
    if run.status == "failed":
        return "DAG execution failed."
    return f"DAG execution {run.status}."


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
