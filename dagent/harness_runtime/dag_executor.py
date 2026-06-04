"""DAG executor with validation, scheduling, and run trace output."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dagent.harness_runtime.artifacts import (
    init_artifact_states,
    resolve_artifact_paths,
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
    CapabilityNodePayload,
    CapabilityResult,
    DAG,
    DAGNode,
    RunTrace,
    RunTraceNode,
    StartNodePayload,
)
from dagent.schemas.value import (
    ArtifactExpr,
    FormatExpr,
    GraphInputExpr,
    NodeOutputExpr,
    parse_value_binding,
)


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
        graph_input: Any = None,
    ) -> None:
        self.capability_executor = capability_executor
        self.partial_node_traces: dict[str, RunTraceNode] = {}
        self.workspace_path = Path(workspace_path).resolve() if workspace_path is not None else None
        self.artifacts = artifacts or {}
        self.artifact_states = artifact_states or init_artifact_states(self.artifacts)
        self.spec_id = spec_id
        self.graph_input = _normalize_graph_input(graph_input)

    async def execute_next_ready_layer(
        self,
        dag: DAG,
        *,
        initial_trace: RunTrace | None = None,
        skills: tuple[str, ...] | None = None,
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
                    skills=skills,
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
        skills: tuple[str, ...] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        pending_nodes = _next_ready_nodes(dag, node_traces)
        if not pending_nodes:
            return
        for node in pending_nodes:
            if isinstance(node.payload, CapabilityNodePayload):
                self._resolve_invocation_values(node, node_traces)
        batch_results = await asyncio.gather(
            *[
                self.execute_node(
                    node,
                    dag,
                    parent_id=trace.root.id,
                    skills=skills,
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

    def _resolve_invocation_values(
        self,
        node: DAGNode,
        node_traces: dict[str, RunTraceNode],
    ) -> None:
        if not isinstance(node.payload, CapabilityNodePayload):
            return
        invocation = node.payload.invocation
        invocation.arguments = _resolve_value(
            invocation.arguments,
            node_traces,
            graph_input=self.graph_input,
            artifacts=self.artifacts,
            workspace_path=self.workspace_path,
        )
        invocation.boundary = invocation.boundary.model_copy(
            update={
                "allowed_paths": _resolve_value_list(
                    invocation.boundary.allowed_paths,
                    node_traces,
                    graph_input=self.graph_input,
                    artifacts=self.artifacts,
                    workspace_path=self.workspace_path,
                ),
                "allowed_commands": _resolve_value_list(
                    invocation.boundary.allowed_commands,
                    node_traces,
                    graph_input=self.graph_input,
                    artifacts=self.artifacts,
                    workspace_path=self.workspace_path,
                ),
            }
        )

    async def execute_node(
        self,
        node: DAGNode,
        dag: DAG,
        *,
        parent_id: str,
        skills: tuple[str, ...] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> RunTraceNode:
        dag_node = RunTraceNode.dag_node(
            parent_id=parent_id,
            node_id=node.id,
            label=node.title or node.id,
        )
        if isinstance(node.payload, StartNodePayload):
            node.status = "completed"
            dag_node.status = "completed"
            dag_node.output = "started"
            dag_node.ended_at = _now()
            return dag_node
        if not isinstance(node.payload, CapabilityNodePayload):
            raise DAGExecutionError(f"Node '{node.id}' has unsupported payload type.")
        node.status = "running"
        return await self.execute_capability_node(
            node,
            dag,
            dag_node=dag_node,
            skills=skills,
            on_token=on_token,
            on_event=on_event,
        )

    async def execute_capability_node(
        self,
        node: DAGNode,
        dag: DAG,
        *,
        dag_node: RunTraceNode,
        skills: tuple[str, ...] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> RunTraceNode:
        if not isinstance(node.payload, CapabilityNodePayload):
            raise DAGExecutionError(f"Node '{node.id}' is not a capability node.")
        invocation = node.payload.invocation
        if not invocation.capability_id:
            raise CapabilityExecutionError(f"Node '{node.id}' has no capability id.")

        try:
            capability_result = await self.capability_executor.execute(
                invocation,
                context=self._execution_context(dag, node, skills=skills),
                callbacks=CapabilityExecutionCallbacks(
                    on_token=on_token,
                    on_event=_node_event_emitter(on_event, dag=dag, node=node, invocation=invocation),
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
        dag_node.value = capability_result.value
        dag_node.step_count = 1
        dag_node.ended_at = _now()
        return dag_node

    def _execution_context(
        self,
        dag: DAG,
        node: DAGNode,
        *,
        skills: tuple[str, ...] | None = None,
    ) -> CapabilityExecutionContext:
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
            skills=skills,
        )

    def _enforce_review_gate(self, dag: DAG) -> None:
        needs_approval = any(
            node.payload.invocation.risk in {"medium", "high"}
            for node in dag.nodes
            if isinstance(node.payload, CapabilityNodePayload)
        )
        if needs_approval and dag.status != "approved":
            raise DAGExecutionError("DAG is not approved for execution.")


def _node_event_emitter(
    on_event: Callable[[dict[str, Any]], None] | None,
    *,
    dag: DAG,
    node: DAGNode,
    invocation: CapabilityInvocation,
) -> Callable[[dict[str, Any]], None] | None:
    if on_event is None:
        return None

    def emit(event: dict[str, Any]) -> None:
        payload = dict(event)
        payload.setdefault("task_id", dag.task_id)
        payload.setdefault("dag_id", dag.dag_id)
        payload.setdefault("node_id", node.id)
        payload.setdefault("parent_capability_id", invocation.capability_id)
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


def _resolve_value(
    value: Any,
    node_traces: dict[str, RunTraceNode],
    *,
    graph_input: Any = None,
    artifacts: dict[str, Artifact] | None = None,
    workspace_path: Path | None = None,
) -> Any:
    expr = parse_value_binding(value)
    if isinstance(expr, GraphInputExpr):
        return _extract_path(graph_input, expr.path)
    if isinstance(expr, NodeOutputExpr):
        return _node_output_value(expr, node_traces)
    if isinstance(expr, ArtifactExpr):
        return _artifact_value(expr, artifacts=artifacts, workspace_path=workspace_path)
    if isinstance(expr, FormatExpr):
        resolved = {
            key: _resolve_value(
                item,
                node_traces,
                graph_input=graph_input,
                artifacts=artifacts,
                workspace_path=workspace_path,
            )
            for key, item in expr.values.items()
        }
        return expr.template.format(**resolved)
    if isinstance(value, dict):
        return {
            key: _resolve_value(
                item,
                node_traces,
                graph_input=graph_input,
                artifacts=artifacts,
                workspace_path=workspace_path,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_value(
                item,
                node_traces,
                graph_input=graph_input,
                artifacts=artifacts,
                workspace_path=workspace_path,
            )
            for item in value
        ]
    return value


def _normalize_graph_input(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _resolve_value_list(
    values: list[Any],
    node_traces: dict[str, RunTraceNode],
    *,
    graph_input: Any,
    artifacts: dict[str, Artifact] | None,
    workspace_path: Path | None,
) -> list[str]:
    resolved: list[str] = []
    for value in values:
        item = _resolve_value(
            value,
            node_traces,
            graph_input=graph_input,
            artifacts=artifacts,
            workspace_path=workspace_path,
        )
        if isinstance(item, list):
            resolved.extend(str(entry) for entry in item)
        else:
            resolved.append(str(item))
    return resolved


def _node_output_value(expr: NodeOutputExpr, node_traces: dict[str, RunTraceNode]) -> Any:
    trace = node_traces.get(expr.node_id)
    if trace is None or trace.status != "completed":
        raise DAGExecutionError(
            f"Cannot resolve output for node '{expr.node_id}' before it completes."
        )
    if expr.field == "value":
        value = trace.value
    elif expr.field == "content":
        value = trace.output
    elif expr.field == "status":
        value = trace.status
    elif expr.field == "steps":
        value = trace.step_count
    else:
        raise DAGExecutionError(f"Unknown node output field '{expr.field}'.")
    return _extract_path(value, expr.path)


def _artifact_value(
    expr: ArtifactExpr,
    *,
    artifacts: dict[str, Artifact] | None,
    workspace_path: Path | None,
) -> Any:
    artifact = (artifacts or {}).get(expr.artifact_id)
    if artifact is None:
        raise DAGExecutionError(f"Unknown artifact '{expr.artifact_id}'.")

    if expr.field == "path":
        return artifact.paths[0]
    if expr.field == "paths":
        return list(artifact.paths)
    if workspace_path is None:
        raise DAGExecutionError(
            f"Cannot resolve absolute artifact '{expr.artifact_id}' without a workspace."
        )
    absolute_paths = [str(path) for path in resolve_artifact_paths(artifact, workspace_path)]
    if expr.field == "absolute_path":
        return absolute_paths[0]
    if expr.field == "absolute_paths":
        return absolute_paths
    raise DAGExecutionError(f"Unknown artifact field '{expr.field}'.")


def _extract_path(value: Any, path: list[str | int]) -> Any:
    current = value
    for item in path:
        try:
            current = current[item]
        except (KeyError, IndexError, TypeError) as exc:
            joined = ".".join(str(part) for part in path)
            raise DAGExecutionError(f"Cannot resolve value path '{joined}'.") from exc
    return current


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
