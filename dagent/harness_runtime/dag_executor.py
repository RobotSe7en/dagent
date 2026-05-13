"""DAG executor with validation, scheduling, and trace."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from dagent.harness_runtime.dag_builder import validate_dag
from dagent.harness_runtime.runtime_trace import TraceRecorder
from dagent.harness_runtime.task_record import ToolExecutionStore
from dagent.schemas import DAG, Boundary, DAGNode, ToolExecutionRecord, TraceEvent
from dagent.tools.boundary import BoundaryViolation
from dagent.tools.executor import ToolExecutor, ToolExecutionError


PLACEHOLDER_PATTERN = re.compile(r"{{\s*([A-Za-z0-9_-]+)\.(output|final_response|status|stop_reason|steps)\s*}}")


class DAGExecutionError(RuntimeError):
    """Raised when a DAG cannot be executed safely."""


@dataclass(frozen=True)
class DAGNodeResult:
    node_id: str
    final_response: str
    completed: bool
    stop_reason: str
    steps: int


@dataclass(frozen=True)
class DAGRunResult:
    dag_id: str
    completed: bool
    node_results: dict[str, DAGNodeResult]
    traces: list[TraceEvent] = field(default_factory=list)
    execution_records: list[ToolExecutionRecord] = field(default_factory=list)


class DAGExecutor:
    """Executes approved DAGs as direct bounded tool-node calls."""

    def __init__(
        self,
        *,
        tool_executor: ToolExecutor,
        trace_recorder: TraceRecorder | None = None,
        execution_store: ToolExecutionStore | None = None,
    ) -> None:
        self.tool_executor = tool_executor
        self.trace_recorder = trace_recorder or TraceRecorder()
        self.execution_store = execution_store or ToolExecutionStore()
        self.partial_node_results: dict[str, DAGNodeResult] = {}

    async def execute_next_ready_layer(
        self,
        dag: DAG,
        *,
        initial_results: dict[str, DAGNodeResult] | None = None,
        record_dag_start: bool = True,
        on_trace: Callable[[TraceEvent], None] | None = None,
    ) -> DAGRunResult:
        """Execute only the next currently-ready DAG layer.

        This is the step-wise execution entrypoint for the dynamic DAG loop.
        It deliberately does not replan yet; later phases can observe the
        returned node records and patch the pending DAG before the next call.
        """
        self.trace_recorder = TraceRecorder(on_record=on_trace)
        self.partial_node_results = {}
        normalized = self.normalize(dag)
        validate_dag(normalized)
        self._enforce_review_gate(normalized)
        normalized.status = "running"
        if record_dag_start:
            self.trace_recorder.record("dag_started", dag_id=normalized.dag_id)
        node_results: dict[str, DAGNodeResult] = dict(initial_results or {})

        permission_result = await self._execute_next_ready_layer(
            normalized,
            node_results,
        )
        if permission_result is not None:
            return permission_result

        completed = _all_nodes_completed(normalized, node_results)
        if completed:
            normalized.status = "completed"
            self.trace_recorder.record(
                "dag_completed",
                dag_id=normalized.dag_id,
                payload={"completed": True},
            )
        return DAGRunResult(
            dag_id=normalized.dag_id,
            completed=completed,
            node_results=node_results,
            traces=list(self.trace_recorder.events),
            execution_records=self.execution_store.records_for_dag(normalized.dag_id),
        )

    async def _execute_next_ready_layer(
        self,
        dag: DAG,
        node_results: dict[str, DAGNodeResult],
    ) -> DAGRunResult | None:
        pending_nodes = _next_ready_nodes(dag, node_results)
        if not pending_nodes:
            return None
        for node in pending_nodes:
            node.invocation.arguments = _inject_placeholders(
                node.invocation.arguments,
                node_results,
            )
            _ensure_no_unresolved_placeholders(node)
        batch_results = await asyncio.gather(
            *[
                self.execute_node(node, dag, node_results)
                for node in pending_nodes
            ],
            return_exceptions=True,
        )
        for result in batch_results:
            if isinstance(result, DAGNodeResult):
                node_results[result.node_id] = result

        for result in batch_results:
            if isinstance(result, Exception):
                dag.status = "failed"
                self.trace_recorder.record("dag_failed", dag_id=dag.dag_id)
                self.partial_node_results = dict(node_results)
                raise result
        return None

    def normalize(self, dag: DAG) -> DAG:
        return dag.model_copy(deep=True)

    async def execute_node(
        self,
        node: DAGNode,
        dag: DAG,
        completed_results: dict[str, DAGNodeResult],
    ) -> DAGNodeResult:
        if node.invocation.tool_name == "dag_start":
            node.status = "completed"
            return DAGNodeResult(
                node_id=node.id,
                final_response="started",
                completed=True,
                stop_reason="completed",
                steps=0,
            )
        self.trace_recorder.record("node_started", dag_id=dag.dag_id, node_id=node.id)
        return self.execute_tool_node(node, dag)

    def execute_tool_node(
        self,
        node: DAGNode,
        dag: DAG,
    ) -> DAGNodeResult:
        if self.tool_executor is None:
            raise ToolExecutionError(
                "DAGExecutor cannot execute tool nodes without a ToolExecutor."
            )
        invocation = node.invocation
        if not invocation.tool_name:
            raise ToolExecutionError(f"Tool node '{node.id}' has no tool name.")

        tool_call_id = invocation.invocation_id
        self.trace_recorder.record(
            "tool_called",
            dag_id=dag.dag_id,
            node_id=node.id,
            payload={
                "tool_call_id": tool_call_id,
                "name": invocation.tool_name,
                "arguments": invocation.arguments,
            },
        )
        try:
            content = self.tool_executor.execute(
                invocation.tool_name,
                invocation.arguments,
                boundary=invocation.boundary,
            )
        except BoundaryViolation as exc:
            _augment_tool_violation(exc, node, self.tool_executor)
            node.status = "failed"
            self.execution_store.add_record(
                task_id=dag.task_id,
                invocation=invocation,
                source="dag_node",
                error=str(exc),
                status="failed",
                stop_reason="boundary_violation",
                steps=1,
                dag=dag,
                node=node,
            )
            self.trace_recorder.record(
                "tool_failed",
                dag_id=dag.dag_id,
                node_id=node.id,
                payload={
                    "tool_call_id": tool_call_id,
                    "name": invocation.tool_name,
                    "error": str(exc),
                },
            )
            self.trace_recorder.record(
                "node_failed",
                dag_id=dag.dag_id,
                node_id=node.id,
                payload={"error": str(exc)},
            )
            raise
        except Exception as exc:
            node.status = "failed"
            self.execution_store.add_record(
                task_id=dag.task_id,
                invocation=invocation,
                source="dag_node",
                error=str(exc),
                status="failed",
                stop_reason="tool_error",
                steps=1,
                dag=dag,
                node=node,
            )
            self.trace_recorder.record(
                "tool_failed",
                dag_id=dag.dag_id,
                node_id=node.id,
                payload={
                    "tool_call_id": tool_call_id,
                    "name": invocation.tool_name,
                    "error": str(exc),
                },
            )
            self.trace_recorder.record(
                "node_failed",
                dag_id=dag.dag_id,
                node_id=node.id,
                payload={"error": str(exc)},
            )
            raise

        self.trace_recorder.record(
            "tool_completed",
            dag_id=dag.dag_id,
            node_id=node.id,
            payload={
                "tool_call_id": tool_call_id,
                "name": invocation.tool_name,
                "content": content,
            },
        )
        self.execution_store.add_record(
            task_id=dag.task_id,
            invocation=invocation,
            source="dag_node",
            output=content,
            status="completed",
            stop_reason="completed",
            steps=1,
            dag=dag,
            node=node,
        )
        result = DAGNodeResult(
            node_id=node.id,
            final_response=content,
            completed=True,
            stop_reason="completed",
            steps=1,
        )
        node.status = "completed"
        self.trace_recorder.record(
            "node_completed",
            dag_id=dag.dag_id,
            node_id=node.id,
            payload={
                "completed": True,
                "stop_reason": result.stop_reason,
                "steps": result.steps,
            },
        )
        return result

    def _enforce_review_gate(self, dag: DAG) -> None:
        needs_approval = any(node.invocation.risk in {"medium", "high"} for node in dag.nodes)
        if needs_approval and dag.status != "approved":
            raise DAGExecutionError("DAG is not approved for execution.")


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
    node_results: dict[str, DAGNodeResult],
) -> list[DAGNode]:
    completed_ids = {
        node_id
        for node_id, result in node_results.items()
        if result.completed
    }
    for batch in _topo_batches(dag):
        pending_nodes = [node for node in batch if node.id not in node_results]
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
    node_results: dict[str, DAGNodeResult],
) -> bool:
    return all(
        node.id in node_results and node_results[node.id].completed
        for node in dag.nodes
    )


def _inject_placeholders(
    value: Any,
    node_results: dict[str, DAGNodeResult],
) -> Any:
    if isinstance(value, dict):
        return {
            key: _inject_placeholders(item, node_results)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_inject_placeholders(item, node_results) for item in value]
    if not isinstance(value, str):
        return value

    exact = PLACEHOLDER_PATTERN.fullmatch(value)
    if exact:
        return _placeholder_value(exact, node_results)

    def replace(match: re.Match[str]) -> str:
        return str(_placeholder_value(match, node_results))

    return PLACEHOLDER_PATTERN.sub(replace, value)


def _placeholder_value(
    match: re.Match[str],
    node_results: dict[str, DAGNodeResult],
) -> Any:
    node_id = match.group(1)
    field = match.group(2)
    result = node_results.get(node_id)
    if result is None or not result.completed:
        raise DAGExecutionError(
            f"Cannot resolve placeholder for node '{node_id}' before it completes."
        )
    if field in {"output", "final_response"}:
        return result.final_response
    if field == "status":
        return "completed" if result.completed else "failed"
    return getattr(result, field)


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


def _augment_tool_violation(
    violation: BoundaryViolation,
    node: DAGNode,
    tool_executor: ToolExecutor,
) -> None:
    invocation = node.invocation
    if invocation.tool_name and not violation.tool_name:
        violation.tool_name = invocation.tool_name
    tool = tool_executor.registry.get(invocation.tool_name) if invocation.tool_name else None
    if tool is None:
        return
    if not violation.action:
        violation.action = tool.action
    if not violation.path:
        for arg_name in tool.path_args:
            value = invocation.arguments.get(arg_name)
            if value is not None:
                violation.path = str(value)
                break
    if not violation.command:
        for arg_name in tool.command_args:
            value = invocation.arguments.get(arg_name)
            if value is not None:
                violation.command = str(value)
                break


