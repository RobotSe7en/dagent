"""DAG executor with validation, scheduling, and run trace output."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
from dagent.harness_runtime.result_storage import normalize_capability_result
from dagent.harness_runtime.execution_budget import ExecutionLimitExceeded
from dagent.harness_runtime.runtime_events import ResponseStreamContext, response_token_stream
from dagent.harness_runtime.dag_builder import (
    compile_dag_spec,
    validate_dag,
    validate_dag_input,
)
from dagent.schemas import (
    Artifact,
    ArtifactState,
    CapabilityInvocation,
    CapabilityNodePayload,
    CapabilityResult,
    ContentReference,
    InlineContent,
    ResultStoragePolicy,
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
from dagent.schemas.common import validate_runtime_directory


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
    capability_workspace_root: Path | None = None
    item: Any = _NO_ITEM


class DAGExecutor:
    """Schedules approved DAG nodes and records execution as a tree."""

    def __init__(
        self,
        *,
        capability_executor: CapabilityExecutor,
        workspace_path: str | Path | None = None,
        capability_workspace_root: str | Path | None = None,
        artifacts: dict[str, Artifact] | None = None,
        artifact_states: dict[str, ArtifactState] | None = None,
        spec_id: str | None = None,
        graph_input: Any = None,
        runtime_directory: str,
        result_storage_policy: ResultStoragePolicy | None = None,
        extra_system_prompt: str | None = None,
    ) -> None:
        self.capability_executor = capability_executor
        self.partial_node_traces: dict[str, RunTraceNode] = {}
        self.workspace_path = Path(workspace_path).resolve() if workspace_path is not None else None
        self.capability_workspace_root = (
            Path(capability_workspace_root).resolve()
            if capability_workspace_root is not None
            else self.capability_executor.workspace_root
        )
        self.artifacts = artifacts or {}
        self.artifact_states = artifact_states or init_artifact_states(self.artifacts)
        self.spec_id = spec_id
        self.graph_input = _normalize_graph_input(graph_input)
        self.runtime_directory = validate_runtime_directory(runtime_directory)
        self.result_storage_policy = result_storage_policy or ResultStoragePolicy()
        self.extra_system_prompt = extra_system_prompt

    def configure_spec(
        self,
        spec: DAGSpec,
        *,
        graph_input: Any,
        artifact_states: dict[str, ArtifactState] | None = None,
    ) -> None:
        """Configure canonical spec data for a dynamic DAG projection."""
        self.spec_id = spec.id
        self.graph_input = _normalize_graph_input(graph_input)
        self.artifacts = {
            artifact_id: artifact.model_copy(deep=True)
            for artifact_id, artifact in spec.artifacts.items()
        }
        previous = artifact_states if artifact_states is not None else self.artifact_states
        initialized = init_artifact_states(self.artifacts)
        for artifact_id, state in previous.items():
            if (
                artifact_id in initialized
                and state.paths == initialized[artifact_id].paths
            ):
                initialized[artifact_id] = state.model_copy(deep=True)
        self.artifact_states = initialized

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
            with self.capability_executor.workspace_context(self.capability_workspace_root):
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
                    self.partial_node_traces[node_id] = result

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
            capability_workspace_root=self.capability_workspace_root,
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
            normalized = normalize_capability_result(
                capability_result,
                workspace_path=self.workspace_path or self.capability_workspace_root,
                runtime_directory=self.runtime_directory,
                policy=self.result_storage_policy,
            )
            capability_result = normalized.result
            stored_content = normalized.content
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
        capability_node.references = normalized.references
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
        dag_node.output = (
            stored_content.text
            if isinstance(stored_content, InlineContent)
            else stored_content.model_dump(mode="json")
        )
        dag_node.output_reference = (
            stored_content if isinstance(stored_content, ContentReference) else None
        )
        dag_node.value = (
            capability_result.value
            if capability_result.value is not None
            else dag_node.output
        )
        dag_node.value_reference = normalized.value_reference
        dag_node.references = normalized.references
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

        async def run_item(
            index: int,
            item: Any,
        ) -> tuple[
            CapabilityInvocation,
            CapabilityResult,
            ContentReference | None,
            tuple[ContentReference, ...],
        ]:
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
                    normalized = normalize_capability_result(
                        result,
                        workspace_path=self.workspace_path or self.capability_workspace_root,
                        runtime_directory=self.runtime_directory,
                        policy=self.result_storage_policy,
                    )
                    result = normalized.result
            finally:
                if token_stream is not None:
                    token_stream.finish()
            return (
                invocation,
                result,
                normalized.value_reference,
                normalized.references,
            )

        outcomes = await asyncio.gather(
            *[run_item(index, item) for index, item in enumerate(items)],
            return_exceptions=True,
        )
        values: list[Any] = []
        value_references: dict[str, ContentReference] = {}
        failure: str | None = None
        for outcome in outcomes:
            if isinstance(outcome, ExecutionLimitExceeded):
                raise outcome
            if isinstance(outcome, BaseException):
                failure = failure or str(outcome)
                continue
            invocation, result, value_reference, references = outcome
            item_node = RunTraceNode.capability_call(
                parent_id=dag_node.id,
                invocation=invocation,
                result=result,
                error=result.error,
            )
            item_node.references = references
            _attach_child_trace(item_node, result)
            dag_node.children.append(item_node)
            if result.status == "failed":
                failure = failure or (result.error or result.content)
            else:
                value_index = len(values)
                values.append(
                    result.value if result.value is not None else result.content
                )
                if value_reference is not None:
                    value_references[f"/{value_index}"] = value_reference
        if failure is not None:
            raise DAGExecutionError(failure)
        dag_node.value_references = value_references
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
        validate_dag_input(spec, graph_input)
        child_dag = compile_dag_spec(spec, task_id=dag.task_id)
        child_dag.status = "approved"
        executor = DAGExecutor(
            capability_executor=self.capability_executor,
            workspace_path=self.workspace_path,
            capability_workspace_root=self.capability_workspace_root,
            runtime_directory=self.runtime_directory,
            result_storage_policy=self.result_storage_policy,
            extra_system_prompt=self.extra_system_prompt,
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
            extra_system_prompt=self.extra_system_prompt,
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
        return _node_output_value(expr, scope)
    if isinstance(expr, ArtifactExpr):
        return _artifact_value(
            expr,
            artifacts=scope.artifacts,
            workspace_path=scope.workspace_path,
            capability_workspace_root=scope.capability_workspace_root,
        )
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


def _node_output_value(expr: NodeOutputExpr, scope: _ValueScope) -> Any:
    trace = scope.node_traces.get(expr.node_id)
    if trace is None or trace.status not in SETTLED_NODE_STATUSES:
        raise DAGExecutionError(
            f"Cannot resolve output for node '{expr.node_id}' before it completes."
        )
    if trace.status == "skipped":
        return "skipped" if expr.field == "status" else None
    if expr.field == "value":
        value = trace.value if trace.value is not None else trace.output
        reference = trace.value_reference
    elif expr.field == "content":
        value = trace.output
        reference = trace.output_reference
    elif expr.field == "status":
        value = trace.status
        reference = None
    elif expr.field == "steps":
        value = trace.step_count
        reference = None
    else:
        raise DAGExecutionError(f"Unknown node output field '{expr.field}'.")
    value = _load_content_reference(value, reference, scope.workspace_path)
    value = _load_indexed_content_references(
        value,
        (
            trace.value_references
            if expr.field in {"value", "content"}
            else {}
        ),
        scope.workspace_path,
    )
    return _extract_path(value, expr.path)


def _load_indexed_content_references(
    value: Any,
    references: dict[str, ContentReference],
    workspace_path: Path | None,
) -> Any:
    if not references:
        return value
    if not isinstance(value, list):
        raise DAGExecutionError(
            "Indexed externalized values require a list node output."
        )
    resolved = list(value)
    for pointer, reference in references.items():
        if not pointer.startswith("/") or not pointer[1:].isdigit():
            raise DAGExecutionError(
                f"Invalid externalized value pointer: {pointer}"
            )
        index = int(pointer[1:])
        if index >= len(resolved):
            raise DAGExecutionError(
                f"Externalized value pointer is out of range: {pointer}"
            )
        resolved[index] = _load_content_reference(
            resolved[index],
            reference,
            workspace_path,
        )
    return resolved


def _load_content_reference(
    value: Any,
    reference: ContentReference | None,
    workspace_path: Path | None,
) -> Any:
    if reference is None:
        return value
    if workspace_path is None:
        raise DAGExecutionError(
            "Cannot resolve an externalized capability result without a run workspace."
        )
    root = workspace_path.expanduser().resolve()
    target = (root / reference.path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise DAGExecutionError(
            "Externalized capability result escapes the run workspace."
        ) from exc
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise DAGExecutionError(
            f"Externalized capability result cannot be read: {reference.path}"
        ) from exc
    if len(data) != reference.byte_length:
        raise DAGExecutionError(
            f"Externalized capability result size mismatch: {reference.path}"
        )
    if hashlib.sha256(data).hexdigest() != reference.sha256:
        raise DAGExecutionError(
            f"Externalized capability result checksum mismatch: {reference.path}"
        )
    media_type = reference.media_type.split(";", 1)[0]
    if media_type == "application/json":
        return json.loads(data.decode("utf-8"))
    if media_type.startswith("text/"):
        return data.decode("utf-8")
    return data


def _artifact_value(
    expr: ArtifactExpr,
    *,
    artifacts: dict[str, Artifact] | None,
    workspace_path: Path | None,
    capability_workspace_root: Path | None,
) -> Any:
    artifact = (artifacts or {}).get(expr.artifact_id)
    if artifact is None:
        raise DAGExecutionError(f"Unknown artifact '{expr.artifact_id}'.")

    if workspace_path is None:
        if expr.field == "path":
            return artifact.paths[0]
        if expr.field == "paths":
            return list(artifact.paths)
        raise DAGExecutionError(
            f"Cannot resolve absolute artifact '{expr.artifact_id}' without a workspace."
        )
    resolved_paths = resolve_artifact_paths(artifact, workspace_path)
    if expr.field == "path":
        return _tool_relative_path(resolved_paths[0], capability_workspace_root)
    if expr.field == "paths":
        return [_tool_relative_path(path, capability_workspace_root) for path in resolved_paths]
    absolute_paths = [str(path) for path in resolved_paths]
    if expr.field == "absolute_path":
        return absolute_paths[0]
    if expr.field == "absolute_paths":
        return absolute_paths
    raise DAGExecutionError(f"Unknown artifact field '{expr.field}'.")


def _tool_relative_path(path: Path, capability_workspace_root: Path | None) -> str:
    if capability_workspace_root is None:
        return str(path)
    try:
        return str(path.relative_to(capability_workspace_root))
    except ValueError:
        raise DAGExecutionError(
            f"Artifact path '{path}' is outside capability workspace '{capability_workspace_root}'. "
            "Use artifact.absolute_path for absolute filesystem paths."
        )


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
