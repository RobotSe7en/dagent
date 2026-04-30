"""DAG executor with validation, risk override, scheduling, and trace."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from dagent.harness_runtime.agent_loop import AgentLoopResult
from dagent.harness_runtime.dag_validation import validate_dag
from dagent.harness_runtime.trace_recorder import TraceRecorder
from dagent.schemas import DAG, Boundary, DAGNode, PermissionRequest, TraceEvent
from dagent.tools.boundary import BoundaryViolation
from dagent.tools.executor import ToolExecutor, ToolExecutionError


class DAGExecutionError(RuntimeError):
    """Raised when a DAG cannot be executed safely."""


class NodeAgentLoop(Protocol):
    async def run(
        self,
        user_message: str,
        *,
        boundary: Boundary,
        max_steps: int = 8,
        allowed_tools: list[str] | None = None,
        messages: list[dict] | None = None,
    ) -> AgentLoopResult:
        """Run one DAG node."""


@dataclass(frozen=True)
class NodeExecutionResult:
    node_id: str
    final_response: str
    completed: bool
    stop_reason: str
    steps: int


class PermissionBlocked(Exception):
    """Internal signal that a node paused for a permission decision."""

    def __init__(
        self,
        node_result: NodeExecutionResult,
        request: PermissionRequest,
    ) -> None:
        super().__init__(request.violation)
        self.node_result = node_result
        self.request = request


@dataclass(frozen=True)
class RunResult:
    dag_id: str
    completed: bool
    node_results: dict[str, NodeExecutionResult]
    traces: list[TraceEvent] = field(default_factory=list)
    pending_permission_request: PermissionRequest | None = None


class DAGExecutor:
    """Executes approved DAGs through bounded node-level agent loops."""

    def __init__(
        self,
        *,
        agent_loop: NodeAgentLoop,
        tool_executor: ToolExecutor | None = None,
        trace_recorder: TraceRecorder | None = None,
    ) -> None:
        self.agent_loop = agent_loop
        self.tool_executor = tool_executor or getattr(agent_loop, "tool_executor", None)
        self.trace_recorder = trace_recorder or TraceRecorder()

    async def execute(
        self,
        dag: DAG,
        *,
        initial_results: dict[str, NodeExecutionResult] | None = None,
    ) -> RunResult:
        self.trace_recorder = TraceRecorder()
        normalized = self.normalize(dag)
        validate_dag(normalized)
        self.apply_risk_overrides(normalized)
        self._enforce_review_gate(normalized)

        normalized.status = "running"
        self.trace_recorder.record("dag_started", dag_id=normalized.dag_id)
        node_results: dict[str, NodeExecutionResult] = dict(initial_results or {})

        for batch in topo_batches(normalized):
            pending_nodes = [node for node in batch if node.id not in node_results]
            if not pending_nodes:
                continue
            batch_results = await asyncio.gather(
                *[
                    self.execute_node(node, normalized, node_results)
                    for node in pending_nodes
                ],
                return_exceptions=True,
            )
            for node, result in zip(pending_nodes, batch_results):
                if isinstance(result, PermissionBlocked):
                    node_results[result.node_result.node_id] = result.node_result
                    normalized.status = "paused_for_permission"
                    blocked_node = _node_by_id(normalized, result.node_result.node_id)
                    blocked_node.status = "blocked_permission"
                    self.trace_recorder.record(
                        "dag_paused",
                        dag_id=normalized.dag_id,
                        payload={
                            "reason": "permission_required",
                            "node_id": result.node_result.node_id,
                        },
                    )
                    return RunResult(
                        dag_id=normalized.dag_id,
                        completed=False,
                        node_results=node_results,
                        traces=list(self.trace_recorder.events),
                        pending_permission_request=result.request,
                    )
                if isinstance(result, Exception):
                    normalized.status = "failed"
                    self.trace_recorder.record("dag_failed", dag_id=normalized.dag_id)
                    raise result
                node_results[result.node_id] = result

        completed = all(result.completed for result in node_results.values())
        normalized.status = "completed" if completed else "failed"
        self.trace_recorder.record(
            "dag_completed" if completed else "dag_failed",
            dag_id=normalized.dag_id,
            payload={"completed": completed},
        )
        return RunResult(
            dag_id=normalized.dag_id,
            completed=completed,
            node_results=node_results,
            traces=list(self.trace_recorder.events),
        )

    def normalize(self, dag: DAG) -> DAG:
        return dag.model_copy(deep=True)

    def apply_risk_overrides(self, dag: DAG) -> None:
        for node in dag.nodes:
            required_risk = self._required_risk_for_node(node)
            if _risk_rank(required_risk) > _risk_rank(node.risk):
                node.risk = required_risk
                suffix = f" Executor override: tools/boundary require {required_risk} risk."
                node.risk_reason = (node.risk_reason + suffix).strip()

    async def execute_node(
        self,
        node: DAGNode,
        dag: DAG,
        completed_results: dict[str, NodeExecutionResult],
    ) -> NodeExecutionResult:
        self.trace_recorder.record("node_started", dag_id=dag.dag_id, node_id=node.id)
        if node.kind == "tool":
            return self.execute_tool_node(node, dag)

        prompt = _node_prompt(node, completed_results)
        try:
            loop_result = await self.agent_loop.run(
                prompt,
                boundary=node.boundary,
                max_steps=node.max_steps,
                allowed_tools=node.tools,
            )
        except BoundaryViolation as exc:
            request = _permission_request_for_violation(dag, node, exc)
            self.trace_recorder.record(
                "permission_requested",
                dag_id=dag.dag_id,
                node_id=node.id,
                payload=request.model_dump(mode="json"),
            )
            self.trace_recorder.record(
                "node_blocked_permission",
                dag_id=dag.dag_id,
                node_id=node.id,
                payload={"error": str(exc), "request_id": request.request_id},
            )
            raise PermissionBlocked(
                NodeExecutionResult(
                    node_id=node.id,
                    final_response="",
                    completed=False,
                    stop_reason="blocked_permission",
                    steps=0,
                ),
                request,
            ) from exc
        except Exception as exc:
            self.trace_recorder.record(
                "node_failed",
                dag_id=dag.dag_id,
                node_id=node.id,
                payload={"error": str(exc)},
            )
            raise

        result = NodeExecutionResult(
            node_id=node.id,
            final_response=loop_result.final_response,
            completed=loop_result.completed,
            stop_reason=loop_result.stop_reason,
            steps=loop_result.steps,
        )
        node.status = "completed" if result.completed else "failed"
        self._record_tool_trace(dag.dag_id, node.id, loop_result.messages)
        self.trace_recorder.record(
            "node_completed" if result.completed else "node_failed",
            dag_id=dag.dag_id,
            node_id=node.id,
            payload={
                "completed": result.completed,
                "stop_reason": result.stop_reason,
                "steps": result.steps,
            },
        )
        return result

    def execute_tool_node(
        self,
        node: DAGNode,
        dag: DAG,
    ) -> NodeExecutionResult:
        if self.tool_executor is None:
            raise ToolExecutionError(
                "DAGExecutor cannot execute tool nodes without a ToolExecutor."
            )
        if not node.tool:
            raise ToolExecutionError(f"Tool node '{node.id}' has no tool name.")

        tool_call_id = f"node_{node.id}"
        self.trace_recorder.record(
            "tool_called",
            dag_id=dag.dag_id,
            node_id=node.id,
            payload={
                "tool_call_id": tool_call_id,
                "name": node.tool,
                "arguments": node.args,
            },
        )
        try:
            content = self.tool_executor.execute(
                node.tool,
                node.args,
                boundary=node.boundary,
            )
        except BoundaryViolation as exc:
            _augment_tool_violation(exc, node, self.tool_executor)
            request = _permission_request_for_violation(dag, node, exc)
            self.trace_recorder.record(
                "permission_requested",
                dag_id=dag.dag_id,
                node_id=node.id,
                payload=request.model_dump(mode="json"),
            )
            self.trace_recorder.record(
                "node_blocked_permission",
                dag_id=dag.dag_id,
                node_id=node.id,
                payload={"error": str(exc), "request_id": request.request_id},
            )
            raise PermissionBlocked(
                NodeExecutionResult(
                    node_id=node.id,
                    final_response="",
                    completed=False,
                    stop_reason="blocked_permission",
                    steps=0,
                ),
                request,
            ) from exc
        except Exception as exc:
            self.trace_recorder.record(
                "tool_failed",
                dag_id=dag.dag_id,
                node_id=node.id,
                payload={
                    "tool_call_id": tool_call_id,
                    "name": node.tool,
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
                "name": node.tool,
                "content": content,
            },
        )
        result = NodeExecutionResult(
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

    def _record_tool_trace(
        self,
        dag_id: str,
        node_id: str,
        messages: list[dict],
    ) -> None:
        for message in messages:
            if message.get("role") == "assistant":
                for tool_call in message.get("tool_calls", []):
                    function = tool_call.get("function", {})
                    self.trace_recorder.record(
                        "tool_called",
                        dag_id=dag_id,
                        node_id=node_id,
                        payload={
                            "tool_call_id": tool_call.get("id"),
                            "name": function.get("name"),
                            "arguments": function.get("arguments"),
                        },
                    )
            if message.get("role") == "tool":
                self.trace_recorder.record(
                    "tool_completed",
                    dag_id=dag_id,
                    node_id=node_id,
                    payload={
                        "tool_call_id": message.get("tool_call_id"),
                        "name": message.get("name"),
                        "content": message.get("content"),
                    },
                )

    def _required_risk_for_node(self, node: DAGNode) -> str:
        risk = "low"
        medium_tools = {"edit_file", "write_file", "shell"}
        high_tools = {"delete_file", "db_write", "deploy", "send_message"}
        node_tools = [node.tool] if node.tool else []
        node_tools.extend(node.tools)

        if any(tool in medium_tools for tool in node_tools):
            risk = _max_risk(risk, "medium")
        if any(tool in high_tools for tool in node_tools):
            risk = _max_risk(risk, "high")
        if node.boundary.allowed_paths in (["."], ["./"]):
            risk = _max_risk(risk, "medium")
        if node.boundary.mode == "full":
            risk = _max_risk(risk, "medium")
        return risk

    def _enforce_review_gate(self, dag: DAG) -> None:
        needs_approval = any(node.risk in {"medium", "high"} for node in dag.nodes)
        if needs_approval and dag.status != "approved":
            raise DAGExecutionError("DAG contains medium/high risk nodes and is not approved.")


def topo_batches(dag: DAG) -> list[list[DAGNode]]:
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


def _node_prompt(
    node: DAGNode,
    completed_results: dict[str, NodeExecutionResult],
) -> str:
    parts = [f"Node goal: {node.goal}"]
    if node.expected_output:
        parts.append(f"Expected output: {node.expected_output}")
    if completed_results:
        parts.append("Prior node results:")
        for result in completed_results.values():
            parts.append(f"- {result.node_id}: {result.final_response}")
    return "\n".join(parts)


def _risk_rank(risk: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}[risk]


def _max_risk(left: str, right: str) -> str:
    return left if _risk_rank(left) >= _risk_rank(right) else right


def _node_by_id(dag: DAG, node_id: str) -> DAGNode:
    for node in dag.nodes:
        if node.id == node_id:
            return node
    raise DAGExecutionError(f"Node '{node_id}' not found.")


def _permission_request_for_violation(
    dag: DAG,
    node: DAGNode,
    violation: BoundaryViolation,
) -> PermissionRequest:
    requested = node.boundary.model_copy(deep=True)
    if violation.action == "write" and requested.mode == "read_only":
        requested.mode = "write_limited"
    if violation.path and violation.path not in requested.allowed_paths:
        requested.allowed_paths.append(violation.path)
    if violation.command:
        executable = _command_executable(violation.command)
        command_grant = executable or violation.command
        if command_grant and command_grant not in requested.allowed_commands:
            requested.allowed_commands.append(command_grant)
    return PermissionRequest(
        request_id=f"perm_{uuid4().hex}",
        dag_id=dag.dag_id,
        node_id=node.id,
        reason=(
            f"Node '{node.id}' needs expanded boundary permissions to continue."
        ),
        violation=str(violation),
        requested_boundary=requested,
    )


def _augment_tool_violation(
    violation: BoundaryViolation,
    node: DAGNode,
    tool_executor: ToolExecutor,
) -> None:
    if node.tool and not violation.tool_name:
        violation.tool_name = node.tool
    tool = tool_executor.registry.get(node.tool) if node.tool else None
    if tool is None:
        return
    if not violation.action:
        violation.action = tool.action
    if not violation.path:
        for arg_name in tool.path_args:
            value = node.args.get(arg_name)
            if value is not None:
                violation.path = str(value)
                break
    if not violation.command:
        for arg_name in tool.command_args:
            value = node.args.get(arg_name)
            if value is not None:
                violation.command = str(value)
                break


def _command_executable(command: str) -> str:
    return command.strip().split(maxsplit=1)[0] if command.strip() else ""
