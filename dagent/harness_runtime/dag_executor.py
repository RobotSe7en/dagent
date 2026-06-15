"""DAG executor with validation, scheduling, and run trace output."""

from __future__ import annotations

import asyncio
import operator
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

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
from dagent.harness_runtime.runtime_events import ResponseStreamContext, response_token_stream
from dagent.harness_runtime.dag_builder import compile_dag_spec, validate_dag
from dagent.schemas import (
    Artifact,
    ArtifactState,
    CapabilityInvocation,
    CapabilityNodePayload,
    CapabilityResult,
    DAG,
    DAGEdge,
    DAGNode,
    DAGSpec,
    LoopNodePayload,
    MapNodePayload,
    RunTrace,
    RunTraceError,
    RunTraceNode,
    StartNodePayload,
    SubgraphNodePayload,
    iter_dag_invocations,
)
from dagent.schemas.value import (
    ArtifactExpr,
    CompareExpr,
    FormatExpr,
    GraphInputExpr,
    ItemExpr,
    NodeOutputExpr,
    parse_value_binding,
)


class DAGExecutionError(RuntimeError):
    """Raised when a DAG cannot be executed safely."""


SETTLED_NODE_STATUSES = frozenset({"completed", "skipped"})
_NO_ITEM = object()
_COMPARE_OPS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "ge": operator.ge,
    "lt": operator.lt,
    "le": operator.le,
}


@dataclass(frozen=True)
class _ValueScope:
    """Everything a value expression can resolve against at one point in time."""

    node_traces: dict[str, RunTraceNode]
    graph_input: Any = None
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    workspace_path: Path | None = None
    item: Any = _NO_ITEM


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
        approve_node_boundaries: bool = False,
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
        node_traces = trace.dag_node_traces()

        try:
            with self.capability_executor.workspace_context(self.workspace_path):
                await self._execute_next_ready_layer(
                    normalized,
                    trace,
                    node_traces,
                    approve_node_boundaries=approve_node_boundaries,
                    skills=skills,
                    on_token=on_token,
                    on_event=on_event,
                )
        except Exception:
            trace.artifacts = dict(self.artifact_states)
            _emit_trace_snapshot(on_event, trace)
            raise

        completed = _all_nodes_settled(normalized, trace.dag_node_traces())
        trace.root.status = "completed" if completed else "running"
        if completed:
            if trace.root.ended_at is None:
                trace.root.ended_at = _now()
            normalized.status = "completed"
        trace.artifacts = dict(self.artifact_states)
        _emit_trace_snapshot(on_event, trace, previous=initial_trace)
        return trace

    async def _execute_next_ready_layer(
        self,
        dag: DAG,
        trace: RunTrace,
        node_traces: dict[str, RunTraceNode],
        *,
        approve_node_boundaries: bool = False,
        skills: tuple[str, ...] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        scope = self._scope(node_traces)
        pending_nodes = _settle_and_ready_nodes(dag, trace, node_traces, scope)
        if not pending_nodes:
            return
        for node in pending_nodes:
            if isinstance(node.payload, CapabilityNodePayload):
                _resolve_invocation(node.payload.invocation, scope)
        batch_results = await asyncio.gather(
            *[
                self.execute_node(
                    node,
                    dag,
                    parent_id=trace.root.id,
                    scope=scope,
                    approve_node_boundaries=approve_node_boundaries,
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
                trace.root.upsert_child(result)
                node_id = result.ref.get("node_id")
                if node_id:
                    node_traces[node_id] = result

        for node_id, partial in self.partial_node_traces.items():
            if node_id not in node_traces:
                trace.root.upsert_child(partial)
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

    def _scope(self, node_traces: dict[str, RunTraceNode], *, item: Any = _NO_ITEM) -> _ValueScope:
        return _ValueScope(
            node_traces=node_traces,
            graph_input=self.graph_input,
            artifacts=self.artifacts,
            workspace_path=self.workspace_path,
            item=item,
        )

    async def execute_node(
        self,
        node: DAGNode,
        dag: DAG,
        *,
        parent_id: str,
        scope: _ValueScope | None = None,
        approve_node_boundaries: bool = False,
        skills: tuple[str, ...] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> RunTraceNode:
        dag_node = RunTraceNode.dag_node(
            parent_id=parent_id,
            node_id=node.id,
            label=node.title or node.id,
        )
        payload = node.payload
        if isinstance(payload, StartNodePayload):
            node.status = "completed"
            dag_node.status = "completed"
            dag_node.output = "started"
            dag_node.ended_at = _now()
            return dag_node
        node.status = "running"
        if isinstance(payload, CapabilityNodePayload):
            return await self.execute_capability_node(
                node,
                dag,
                dag_node=dag_node,
                approve_node_boundaries=approve_node_boundaries,
                skills=skills,
                on_token=on_token,
                on_event=on_event,
            )
        if scope is None:
            scope = self._scope({})
        try:
            if isinstance(payload, MapNodePayload):
                value = await self._execute_map_node(
                    node, dag, payload, dag_node=dag_node, scope=scope,
                    approve_node_boundaries=approve_node_boundaries,
                    skills=skills, on_token=on_token, on_event=on_event,
                )
            elif isinstance(payload, SubgraphNodePayload):
                value = await self._execute_subgraph_node(
                    payload, dag, dag_node=dag_node, scope=scope,
                    approve_node_boundaries=approve_node_boundaries,
                    skills=skills, on_token=on_token, on_event=on_event,
                )
            elif isinstance(payload, LoopNodePayload):
                value = await self._execute_loop_node(
                    payload, dag, dag_node=dag_node, scope=scope,
                    approve_node_boundaries=approve_node_boundaries,
                    skills=skills, on_token=on_token, on_event=on_event,
                )
            else:
                raise DAGExecutionError(f"Node '{node.id}' has unsupported payload type.")
        except Exception as exc:
            node.status = "failed"
            dag_node.status = "failed"
            dag_node.error = _error(str(exc), type(exc).__name__)
            dag_node.ended_at = _now()
            self.partial_node_traces[node.id] = dag_node
            raise
        if self.workspace_path is not None and self.artifacts:
            update_node_output_artifacts(
                node,
                artifacts=self.artifacts,
                states=self.artifact_states,
                workspace_path=self.workspace_path,
            )
        node.status = "completed"
        dag_node.status = "completed"
        dag_node.output = value
        dag_node.value = value
        dag_node.step_count = max(len(dag_node.children), 1)
        dag_node.ended_at = _now()
        return dag_node

    async def execute_capability_node(
        self,
        node: DAGNode,
        dag: DAG,
        *,
        dag_node: RunTraceNode,
        approve_node_boundaries: bool = False,
        skills: tuple[str, ...] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> RunTraceNode:
        if not isinstance(node.payload, CapabilityNodePayload):
            raise DAGExecutionError(f"Node '{node.id}' is not a capability node.")
        invocation = node.payload.invocation
        if not invocation.capability_id:
            raise CapabilityExecutionError(f"Node '{node.id}' has no capability id.")

        node_event_emitter = _node_event_emitter(on_event, dag=dag, node=node, invocation=invocation)
        token_stream = None
        if invocation.kind != "agent":
            token_stream = response_token_stream(
                on_raw=on_token,
                on_event=node_event_emitter,
                context=ResponseStreamContext.create(
                    run_id=dag.task_id,
                    dag_id=dag.dag_id,
                    node_id=node.id,
                    parent_capability_id=invocation.capability_id,
                ),
            )

        try:
            capability_result = await self.capability_executor.execute(
                invocation,
                context=self._execution_context(
                    dag,
                    node,
                    approved_boundary_invocation_id=(
                        invocation.invocation_id if approve_node_boundaries else None
                    ),
                    skills=skills,
                ),
                callbacks=CapabilityExecutionCallbacks(
                    on_token=token_stream or on_token,
                    on_event=node_event_emitter,
                ),
            )
        except Exception as exc:
            node.status = "failed"
            failed_result = CapabilityResult.failed(invocation, str(exc), stop_reason=type(exc).__name__)
            capability_node = RunTraceNode.capability_call(
                parent_id=dag_node.id,
                invocation=invocation,
                result=failed_result,
                error=str(exc),
            )
            dag_node.children.append(capability_node)
            dag_node.status = "failed"
            dag_node.error = _error(str(exc), type(exc).__name__)
            dag_node.ended_at = _now()
            self.partial_node_traces[node.id] = dag_node
            raise
        finally:
            if token_stream is not None:
                token_stream.finish()

        capability_node = RunTraceNode.capability_call(
            parent_id=dag_node.id,
            invocation=invocation,
            result=capability_result,
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

    async def _execute_map_node(
        self,
        node: DAGNode,
        dag: DAG,
        payload: MapNodePayload,
        *,
        dag_node: RunTraceNode,
        scope: _ValueScope,
        approve_node_boundaries: bool = False,
        skills: tuple[str, ...] | None,
        on_token: Callable[[str], None] | None,
        on_event: Callable[[dict[str, Any]], None] | None,
    ) -> list[Any]:
        items = _resolve_value(payload.items, scope)
        if not isinstance(items, list):
            raise DAGExecutionError(
                f"Map node '{node.id}' items must resolve to a list, got {type(items).__name__}."
            )
        if len(items) > payload.max_items:
            raise DAGExecutionError(
                f"Map node '{node.id}' resolved {len(items)} items, exceeding max_items={payload.max_items}."
            )
        semaphore = asyncio.Semaphore(payload.max_concurrency)

        async def run_item(index: int, item: Any) -> tuple[CapabilityInvocation, CapabilityResult]:
            invocation = payload.invocation.model_copy(
                deep=True,
                update={"invocation_id": f"{node.id}_item{index}_{uuid4().hex[:8]}"},
            )
            _resolve_invocation(invocation, replace(scope, item=item))
            emitter = _node_event_emitter(on_event, dag=dag, node=node, invocation=invocation)
            token_stream = response_token_stream(
                on_raw=on_token,
                on_event=emitter,
                context=ResponseStreamContext.create(
                    run_id=dag.task_id,
                    dag_id=dag.dag_id,
                    node_id=node.id,
                    parent_capability_id=invocation.capability_id,
                ),
            )
            # Per-item node identity keeps stateful handlers (agent sessions) isolated.
            context = replace(
                self._execution_context(
                    dag,
                    node,
                    approved_boundary_invocation_id=(
                        invocation.invocation_id if approve_node_boundaries else None
                    ),
                    skills=skills,
                ),
                node=node.model_copy(update={"id": f"{node.id}[{index}]"}),
            )
            try:
                async with semaphore:
                    result = await self.capability_executor.execute(
                        invocation,
                        context=context,
                        callbacks=CapabilityExecutionCallbacks(
                            on_token=token_stream or on_token,
                            on_event=emitter,
                        ),
                    )
            finally:
                if token_stream is not None:
                    token_stream.finish()
            return invocation, result

        outcomes = await asyncio.gather(
            *[run_item(index, item) for index, item in enumerate(items)],
            return_exceptions=True,
        )
        values: list[Any] = []
        failure: str | None = None
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                failure = failure or str(outcome)
                continue
            invocation, result = outcome
            item_node = RunTraceNode.capability_call(
                parent_id=dag_node.id,
                invocation=invocation,
                result=result,
                error=result.error,
            )
            _attach_child_trace(item_node, result)
            dag_node.children.append(item_node)
            if result.status == "failed":
                failure = failure or (result.error or result.content)
            else:
                values.append(result.value if result.value is not None else result.content)
        if failure is not None:
            raise DAGExecutionError(failure)
        return values

    async def _execute_subgraph_node(
        self,
        payload: SubgraphNodePayload,
        dag: DAG,
        *,
        dag_node: RunTraceNode,
        scope: _ValueScope,
        approve_node_boundaries: bool = False,
        skills: tuple[str, ...] | None,
        on_token: Callable[[str], None] | None,
        on_event: Callable[[dict[str, Any]], None] | None,
    ) -> Any:
        return await self._run_embedded_spec(
            payload.spec,
            graph_input=_resolve_value(payload.input, scope),
            dag=dag,
            dag_node=dag_node,
            label=payload.spec.name or payload.spec.id,
            approve_node_boundaries=approve_node_boundaries,
            skills=skills,
            on_token=on_token,
            on_event=on_event,
        )

    async def _execute_loop_node(
        self,
        payload: LoopNodePayload,
        dag: DAG,
        *,
        dag_node: RunTraceNode,
        scope: _ValueScope,
        approve_node_boundaries: bool = False,
        skills: tuple[str, ...] | None,
        on_token: Callable[[str], None] | None,
        on_event: Callable[[dict[str, Any]], None] | None,
    ) -> Any:
        current = _resolve_value(payload.input, scope)
        value: Any = None
        for iteration in range(1, payload.max_iterations + 1):
            value = await self._run_embedded_spec(
                payload.body,
                graph_input=current,
                dag=dag,
                dag_node=dag_node,
                label=f"{payload.body.name or payload.body.id} #{iteration}",
                approve_node_boundaries=approve_node_boundaries,
                skills=skills,
                on_token=on_token,
                on_event=on_event,
            )
            if bool(_resolve_value(payload.until, replace(scope, item=value))):
                break
            current = value
        return value

    async def _run_embedded_spec(
        self,
        spec: DAGSpec,
        *,
        graph_input: Any,
        dag: DAG,
        dag_node: RunTraceNode,
        label: str,
        approve_node_boundaries: bool = False,
        skills: tuple[str, ...] | None,
        on_token: Callable[[str], None] | None,
        on_event: Callable[[dict[str, Any]], None] | None,
    ) -> Any:
        """Run one embedded spec to completion; its trace nests under ``dag_node``."""
        child_dag = compile_dag_spec(spec, task_id=dag.task_id)
        child_dag.status = "approved"
        executor = DAGExecutor(
            capability_executor=self.capability_executor,
            workspace_path=self.workspace_path,
            artifacts=spec.artifacts,
            spec_id=spec.id,
            graph_input=graph_input,
        )
        child_on_event = _without_trace_events(on_event)
        trace: RunTrace | None = None
        try:
            while True:
                settled_before = len(trace.dag_node_traces()) if trace is not None else 0
                trace = await executor.execute_next_ready_layer(
                    child_dag,
                    initial_trace=trace,
                    approve_node_boundaries=approve_node_boundaries,
                    skills=skills,
                    on_token=on_token,
                    on_event=child_on_event,
                )
                if trace.root.status == "completed":
                    break
                if len(trace.dag_node_traces()) == settled_before:
                    raise DAGExecutionError(f"Embedded DAG '{spec.id}' made no progress.")
        finally:
            if trace is not None:
                child_root = trace.root
                # The deterministic run-root id repeats across iterations; re-key it.
                child_root.id = f"trace_node_{uuid4().hex}"
                child_root.parent_id = dag_node.id
                child_root.label = label
                child_root.reparent_children()
                dag_node.children.append(child_root)
        if spec.output is None:
            return None
        return executor.resolve_spec_output(spec.output, trace)

    def resolve_spec_output(self, output: Any, trace: RunTrace) -> Any:
        """Resolve a ``DAGSpec.output`` expression against this executor's run."""
        return _resolve_value(output, self._scope(trace.dag_node_traces()))

    def _execution_context(
        self,
        dag: DAG,
        node: DAGNode,
        *,
        approved_boundary_invocation_id: str | None = None,
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
            approved_boundary_invocation_id=approved_boundary_invocation_id,
        )

    def _enforce_review_gate(self, dag: DAG) -> None:
        needs_approval = any(
            invocation.risk in {"medium", "high"}
            for invocation in iter_dag_invocations(dag.nodes)
        )
        if needs_approval and dag.status != "approved":
            raise DAGExecutionError("DAG is not approved for execution.")


def _resolve_invocation(invocation: CapabilityInvocation, scope: _ValueScope) -> None:
    """Resolve value expressions in an invocation's arguments and boundary, in place."""
    invocation.arguments = _resolve_value(invocation.arguments, scope)
    invocation.boundary = invocation.boundary.model_copy(
        update={
            "allowed_paths": _resolve_value_list(invocation.boundary.allowed_paths, scope),
            "allowed_commands": _resolve_value_list(invocation.boundary.allowed_commands, scope),
        }
    )


def _without_trace_events(
    on_event: Callable[[dict[str, Any]], None] | None,
) -> Callable[[dict[str, Any]], None] | None:
    """Suppress embedded-run trace snapshots; the parent emits the combined trace."""
    if on_event is None:
        return None

    def emit(event: dict[str, Any]) -> None:
        if event.get("type") != "trace":
            on_event(event)

    return emit


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
        if payload.get("run_id") is None:
            payload["run_id"] = dag.task_id
        if payload.get("dag_id") is None:
            payload["dag_id"] = dag.dag_id
        if payload.get("node_id") is None:
            payload["node_id"] = node.id
        if payload.get("parent_capability_id") is None:
            payload["parent_capability_id"] = invocation.capability_id
        on_event(payload)

    return emit


def _emit_trace_snapshot(
    on_event: Callable[[dict[str, Any]], None] | None,
    trace: RunTrace,
    *,
    previous: RunTrace | None = None,
) -> None:
    if on_event is None:
        return
    payload = trace.model_dump(mode="json")
    if previous is not None and payload == previous.model_dump(mode="json"):
        return
    on_event({"type": "trace", "trace": payload})


def _settle_and_ready_nodes(
    dag: DAG,
    trace: RunTrace,
    node_traces: dict[str, RunTraceNode],
    scope: _ValueScope,
) -> list[DAGNode]:
    """Skip nodes whose every incoming edge is dead, then return executable nodes.

    An edge is live when its source completed and its ``when`` condition (if any)
    is truthy; edges from skipped sources are dead. Skips cascade to a fixpoint.
    """
    incoming: dict[str, list[DAGEdge]] = defaultdict(list)
    for edge in dag.edges:
        incoming[edge.target].append(edge)

    def settled(node: DAGNode) -> bool:
        return all(
            edge.source in node_traces and node_traces[edge.source].status in SETTLED_NODE_STATUSES
            for edge in incoming[node.id]
        )

    changed = True
    while changed:
        changed = False
        for node in dag.nodes:
            if node.id in node_traces or not incoming[node.id] or not settled(node):
                continue
            if any(_edge_live(edge, node_traces, scope) for edge in incoming[node.id]):
                continue
            node.status = "skipped"
            skipped = RunTraceNode.dag_node(
                parent_id=trace.root.id,
                node_id=node.id,
                status="skipped",
                label=node.title or node.id,
            )
            skipped.ended_at = _now()
            trace.root.upsert_child(skipped)
            node_traces[node.id] = skipped
            changed = True

    return [node for node in dag.nodes if node.id not in node_traces and settled(node)]


def _edge_live(
    edge: DAGEdge,
    node_traces: dict[str, RunTraceNode],
    scope: _ValueScope,
) -> bool:
    source = node_traces.get(edge.source)
    if source is None or source.status != "completed":
        return False
    if edge.when is None:
        return True
    return bool(_resolve_value(edge.when, scope))


def _all_nodes_settled(
    dag: DAG,
    node_traces: dict[str, RunTraceNode],
) -> bool:
    return all(
        node.id in node_traces and node_traces[node.id].status in SETTLED_NODE_STATUSES
        for node in dag.nodes
    )


def _resolve_value(value: Any, scope: _ValueScope) -> Any:
    expr = parse_value_binding(value)
    if isinstance(expr, GraphInputExpr):
        return _extract_path(scope.graph_input, expr.path)
    if isinstance(expr, NodeOutputExpr):
        return _node_output_value(expr, scope.node_traces)
    if isinstance(expr, ArtifactExpr):
        return _artifact_value(expr, artifacts=scope.artifacts, workspace_path=scope.workspace_path)
    if isinstance(expr, FormatExpr):
        resolved = {key: _resolve_value(item, scope) for key, item in expr.values.items()}
        return expr.template.format(**resolved)
    if isinstance(expr, CompareExpr):
        return _COMPARE_OPS[expr.op](
            _resolve_value(expr.left, scope),
            _resolve_value(expr.right, scope),
        )
    if isinstance(expr, ItemExpr):
        if scope.item is _NO_ITEM:
            raise DAGExecutionError(
                "item expressions are only valid inside map nodes and loop conditions."
            )
        return _extract_path(scope.item, expr.path)
    if isinstance(value, dict):
        return {key: _resolve_value(item, scope) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item, scope) for item in value]
    return value


def _normalize_graph_input(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _resolve_value_list(values: list[Any], scope: _ValueScope) -> list[str]:
    resolved: list[str] = []
    for value in values:
        item = _resolve_value(value, scope)
        if isinstance(item, list):
            resolved.extend(str(entry) for entry in item)
        else:
            resolved.append(str(item))
    return resolved


def _node_output_value(expr: NodeOutputExpr, node_traces: dict[str, RunTraceNode]) -> Any:
    trace = node_traces.get(expr.node_id)
    if trace is None or trace.status not in SETTLED_NODE_STATUSES:
        raise DAGExecutionError(
            f"Cannot resolve output for node '{expr.node_id}' before it completes."
        )
    if trace.status == "skipped":
        return "skipped" if expr.field == "status" else None
    if expr.field == "value":
        value = trace.value if trace.value is not None else trace.output
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
    agent_loop.reparent_children()
    capability_node.children.append(agent_loop)


def _error(message: str, code: str) -> RunTraceError:
    return RunTraceError(message=message, code=code)


def _now() -> datetime:
    return datetime.now(timezone.utc)
