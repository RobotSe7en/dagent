"""DAG executor with validation, scheduling, and run trace output."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dagent.harness_runtime.artifacts import (
    init_artifact_states,
    resolve_node_artifacts,
    update_node_output_artifacts,
)
from dagent.harness_runtime.capability_executor import (
    CapabilityExecutionCallbacks,
    CapabilityExecutionContext,
    CapabilityExecutionError,
    CapabilityExecutor,
)
from dagent.harness_runtime.dag_builder import validate_dag
from dagent.schemas import (
    Artifact,
    ArtifactState,
    CapabilityInvocation,
    CapabilityResult,
    DAG,
    DAGNode,
    RunTrace,
    RunTraceNode,
)


PLACEHOLDER_PATTERN = re.compile(r"{{\s*([A-Za-z0-9_-]+)\.(output|final_response|status|stop_reason|steps)\s*}}")


class DAGExecutionError(RuntimeError):
    """Raised when a DAG cannot be executed safely."""


class DAGExecutor:
    """Schedules approved DAG nodes and records execution as a tree."""

    def __init__(
        self,
        *,
        capability_executor: CapabilityExecutor,
        workspace_path: str | Path | None = None,
        artifacts: dict[str, Artifact] | None = None,
        artifact_states: dict[str, ArtifactState] | None = None,
        spec_id: str | None = None,
    ) -> None:
        self.capability_executor = capability_executor
        self.partial_node_traces: dict[str, RunTraceNode] = {}
        self.workspace_path = Path(workspace_path).resolve() if workspace_path is not None else None
        self.artifacts = artifacts or {}
        self.artifact_states = artifact_states or init_artifact_states(self.artifacts)
        self.spec_id = spec_id

    async def execute_next_ready_layer(
        self,
        dag: DAG,
        *,
        initial_trace: RunTrace | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> RunTrace:
        """Execute the next currently-ready DAG layer and return cumulative run trace."""
        self.partial_node_traces = {}
        normalized = self.normalize(dag)
        validate_dag(normalized)
        self._enforce_review_gate(normalized)
        normalized.status = "running"
        trace = _copy_or_create_trace(initial_trace, normalized)
        node_traces = _node_traces_by_id(trace)

        try:
            with self.capability_executor.workspace_context(self.workspace_path):
                await self._execute_next_ready_layer(
                    normalized,
                    trace,
                    node_traces,
                    on_token=on_token,
                    on_event=on_event,
                )
        except Exception:
            trace.artifacts = dict(self.artifact_states)
            _emit_trace_snapshot(on_event, trace)
            raise

        completed = _all_nodes_completed(normalized, _node_traces_by_id(trace))
        trace.root.status = "completed" if completed else "running"
        if completed:
            trace.root.ended_at = _now()
            normalized.status = "completed"
        trace.artifacts = dict(self.artifact_states)
        _emit_trace_snapshot(on_event, trace)
        return trace

    async def _execute_next_ready_layer(
        self,
        dag: DAG,
        trace: RunTrace,
        node_traces: dict[str, RunTraceNode],
        *,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        pending_nodes = _next_ready_nodes(dag, node_traces)
        if not pending_nodes:
            return
        for node in pending_nodes:
            node.invocation.arguments = _inject_placeholders(
                node.invocation.arguments,
                node_traces,
            )
            _ensure_no_unresolved_placeholders(node)
        batch_results = await asyncio.gather(
            *[
                self.execute_node(
                    node,
                    dag,
                    parent_id=trace.root.id,
                    on_token=on_token,
                    on_event=on_event,
                )
                for node in pending_nodes
            ],
            return_exceptions=True,
        )
        for result in batch_results:
            if isinstance(result, RunTraceNode):
                _replace_or_append_child(trace.root, result)
                node_id = result.ref.get("node_id")
                if node_id:
                    node_traces[node_id] = result

        for node_id, partial in self.partial_node_traces.items():
            if node_id not in node_traces:
                _replace_or_append_child(trace.root, partial)
                node_traces[node_id] = partial

        for result in batch_results:
            if isinstance(result, Exception):
                dag.status = "failed"
                trace.root.status = "failed"
                trace.root.error = _error(str(result), type(result).__name__)
                trace.root.ended_at = _now()
                raise result

    def normalize(self, dag: DAG) -> DAG:
        return dag.model_copy(deep=True)

    async def execute_node(
        self,
        node: DAGNode,
        dag: DAG,
        *,
        parent_id: str,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> RunTraceNode:
        dag_node = RunTraceNode.dag_node(
            parent_id=parent_id,
            node_id=node.id,
            label=node.title or node.id,
        )
        if node.node_type == "start":
            node.status = "completed"
            dag_node.status = "completed"
            dag_node.output = "started"
            dag_node.ended_at = _now()
            return dag_node
        node.status = "running"
        return await self.execute_capability_node(
            node,
            dag,
            dag_node=dag_node,
            on_token=on_token,
            on_event=on_event,
        )

    async def execute_capability_node(
        self,
        node: DAGNode,
        dag: DAG,
        *,
        dag_node: RunTraceNode,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> RunTraceNode:
        invocation = node.invocation
        if not invocation.capability_id:
            raise CapabilityExecutionError(f"Node '{node.id}' has no capability id.")

        try:
            capability_result = await self.capability_executor.execute(
                invocation,
                context=self._execution_context(dag, node),
                callbacks=CapabilityExecutionCallbacks(
                    on_token=on_token,
                    on_event=_node_event_emitter(on_event, dag=dag, node=node),
                ),
            )
        except Exception as exc:
            node.status = "failed"
            failed_result = _failed_capability_result(invocation, str(exc), type(exc).__name__)
            capability_node = RunTraceNode.capability_call(
                parent_id=dag_node.id,
                invocation=invocation,
                result=failed_result,
                output="",
                error=str(exc),
            )
            dag_node.children.append(capability_node)
            dag_node.status = "failed"
            dag_node.error = _error(str(exc), type(exc).__name__)
            dag_node.ended_at = _now()
            self.partial_node_traces[node.id] = dag_node
            raise

        capability_node = RunTraceNode.capability_call(
            parent_id=dag_node.id,
            invocation=invocation,
            result=capability_result,
            output=capability_result.content,
            error=capability_result.error,
        )
        _attach_child_trace(capability_node, capability_result)
        dag_node.children.append(capability_node)

        if capability_result.status == "failed":
            error = capability_result.error or capability_result.content
            stop_reason = (
                "boundary_violation"
                if capability_result.stop_reason == "BoundaryViolation"
                else "capability_error"
            )
            node.status = "failed"
            dag_node.status = "failed"
            dag_node.error = _error(error, stop_reason)
            dag_node.ended_at = _now()
            self.partial_node_traces[node.id] = dag_node
            raise DAGExecutionError(error)

        if self.workspace_path is not None and self.artifacts:
            update_node_output_artifacts(
                node,
                artifacts=self.artifacts,
                states=self.artifact_states,
                workspace_path=self.workspace_path,
            )
        node.status = "completed"
        dag_node.status = "completed"
        dag_node.output = capability_result.content
        dag_node.step_count = 1
        dag_node.ended_at = _now()
        return dag_node

    def _execution_context(self, dag: DAG, node: DAGNode) -> CapabilityExecutionContext:
        input_artifacts: dict[str, list[Path]] = {}
        output_artifacts: dict[str, list[Path]] = {}
        if self.workspace_path is not None and self.artifacts:
            input_artifacts, output_artifacts = resolve_node_artifacts(
                node,
                artifacts=self.artifacts,
                workspace_path=self.workspace_path,
            )
        return CapabilityExecutionContext(
            task_id=dag.task_id,
            dag_id=dag.dag_id,
            spec_id=self.spec_id,
            node=node.model_copy(deep=True),
            workspace_path=self.workspace_path,
            input_artifacts=input_artifacts,
            output_artifacts=output_artifacts,
            artifact_states=dict(self.artifact_states),
        )

    def _enforce_review_gate(self, dag: DAG) -> None:
        needs_approval = any(node.invocation.risk in {"medium", "high"} for node in dag.nodes)
        if needs_approval and dag.status != "approved":
            raise DAGExecutionError("DAG is not approved for execution.")


def _node_event_emitter(
    on_event: Callable[[dict[str, Any]], None] | None,
    *,
    dag: DAG,
    node: DAGNode,
) -> Callable[[dict[str, Any]], None] | None:
    if on_event is None:
        return None

    def emit(event: dict[str, Any]) -> None:
        payload = dict(event)
        payload.setdefault("task_id", dag.task_id)
        payload.setdefault("dag_id", dag.dag_id)
        payload.setdefault("node_id", node.id)
        payload.setdefault("parent_capability_id", node.invocation.capability_id)
        on_event(payload)

    return emit


def _emit_trace_snapshot(
    on_event: Callable[[dict[str, Any]], None] | None,
    trace: RunTrace,
) -> None:
    if on_event is not None:
        on_event({"type": "trace", "trace": trace.model_dump(mode="json")})


def _topo_batches(dag: DAG) -> list[list[DAGNode]]:
    nodes_by_id = {node.id: node for node in dag.nodes}
    outgoing: dict[str, list[str]] = defaultdict(list)
    indegree = {node.id: 0 for node in dag.nodes}

    for edge in dag.edges:
        outgoing[edge.source].append(edge.target)
        indegree[edge.target] += 1

    ready = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    batches: list[list[DAGNode]] = []

    while ready:
        current_batch_ids = list(ready)
        ready.clear()
        batches.append([nodes_by_id[node_id] for node_id in current_batch_ids])

        for node_id in current_batch_ids:
            for target in sorted(outgoing[node_id]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)

    return batches


def _next_ready_nodes(
    dag: DAG,
    node_traces: dict[str, RunTraceNode],
) -> list[DAGNode]:
    completed_ids = {
        node_id
        for node_id, trace in node_traces.items()
        if trace.status == "completed"
    }
    for batch in _topo_batches(dag):
        pending_nodes = [node for node in batch if node.id not in node_traces]
        if not pending_nodes:
            continue
        ready = [
            node for node in pending_nodes
            if all(
                edge.source in completed_ids
                for edge in dag.edges
                if edge.target == node.id
            )
        ]
        if ready:
            return ready
    return []


def _all_nodes_completed(
    dag: DAG,
    node_traces: dict[str, RunTraceNode],
) -> bool:
    return all(
        node.id in node_traces and node_traces[node.id].status == "completed"
        for node in dag.nodes
    )


def _inject_placeholders(
    value: Any,
    node_traces: dict[str, RunTraceNode],
) -> Any:
    if isinstance(value, dict):
        return {
            key: _inject_placeholders(item, node_traces)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_inject_placeholders(item, node_traces) for item in value]
    if not isinstance(value, str):
        return value

    exact = PLACEHOLDER_PATTERN.fullmatch(value)
    if exact:
        return _placeholder_value(exact, node_traces)

    def replace(match: re.Match[str]) -> str:
        return str(_placeholder_value(match, node_traces))

    return PLACEHOLDER_PATTERN.sub(replace, value)


def _placeholder_value(
    match: re.Match[str],
    node_traces: dict[str, RunTraceNode],
) -> Any:
    node_id = match.group(1)
    field = match.group(2)
    trace = node_traces.get(node_id)
    if trace is None or trace.status != "completed":
        raise DAGExecutionError(
            f"Cannot resolve placeholder for node '{node_id}' before it completes."
        )
    if field in {"output", "final_response"}:
        return trace.output
    if field == "status":
        return trace.status
    if field == "stop_reason":
        return "completed" if trace.status == "completed" else "failed"
    if field == "steps":
        return trace.step_count
    raise DAGExecutionError(f"Unknown placeholder field '{field}'.")


def _ensure_no_unresolved_placeholders(node: DAGNode) -> None:
    unresolved = _find_unresolved_placeholders(node.invocation.arguments)
    if unresolved:
        joined = ", ".join(sorted(unresolved))
        raise DAGExecutionError(
            f"Node '{node.id}' has unresolved placeholder(s): {joined}."
        )


def _find_unresolved_placeholders(value: Any) -> set[str]:
    if isinstance(value, dict):
        found: set[str] = set()
        for item in value.values():
            found.update(_find_unresolved_placeholders(item))
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for item in value:
            found.update(_find_unresolved_placeholders(item))
        return found
    if isinstance(value, str):
        return set(re.findall(r"{{[^{}]+}}", value))
    return set()


def _copy_or_create_trace(trace: RunTrace | None, dag: DAG) -> RunTrace:
    if trace is not None:
        return trace.model_copy(deep=True)
    root = RunTraceNode.run(run_id=dag.task_id, status="running")
    root.ref["dag_id"] = dag.dag_id
    return RunTrace(run_id=dag.task_id, root=root)


def _node_traces_by_id(trace: RunTrace) -> dict[str, RunTraceNode]:
    return {
        node.ref["node_id"]: node
        for node in trace.root.children
        if node.kind == "dag_node" and node.ref.get("node_id")
    }


def _replace_or_append_child(parent: RunTraceNode, child: RunTraceNode) -> None:
    child_ref = child.ref
    for index, existing in enumerate(parent.children):
        if existing.kind == child.kind and existing.ref == child_ref:
            parent.children[index] = child
            return
    parent.children.append(child)


def _failed_capability_result(
    invocation: CapabilityInvocation,
    error: str,
    stop_reason: str,
) -> CapabilityResult:
    return CapabilityResult(
        invocation_id=invocation.invocation_id,
        capability_id=invocation.capability_id,
        kind=invocation.kind,
        status="failed",
        error=error,
        stop_reason=stop_reason,
    )


def _attach_child_trace(
    capability_node: RunTraceNode,
    capability_result: CapabilityResult,
) -> None:
    if not capability_result.trace:
        return
    child_trace = RunTrace.model_validate(capability_result.trace)
    agent_loop = RunTraceNode(
        parent_id=capability_node.id,
        kind="agent_loop",
        status=child_trace.root.status,
        label="agent_loop",
        output=child_trace.root.output,
        children=child_trace.root.children,
    )
    _reparent(agent_loop)
    capability_node.children.append(agent_loop)


def _reparent(parent: RunTraceNode) -> None:
    for child in parent.children:
        child.parent_id = parent.id
        _reparent(child)


def _error(message: str, code: str):
    from dagent.schemas import RunTraceError

    return RunTraceError(message=message, code=code)


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
