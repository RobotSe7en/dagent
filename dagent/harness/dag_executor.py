"""DAG executor with validation, risk override, scheduling, and trace."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Protocol

from dagent.harness.dag_validation import validate_dag
from dagent.harness.trace_recorder import TraceRecorder
from dagent.runtime import AgentLoopResult
from dagent.schemas import DAG, Boundary, DAGNode, TraceEvent


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


@dataclass(frozen=True)
class RunResult:
    dag_id: str
    completed: bool
    node_results: dict[str, NodeExecutionResult]
    traces: list[TraceEvent] = field(default_factory=list)


class DAGExecutor:
    """Executes approved DAGs through bounded node-level agent loops."""

    def __init__(
        self,
        *,
        agent_loop: NodeAgentLoop,
        trace_recorder: TraceRecorder | None = None,
    ) -> None:
        self.agent_loop = agent_loop
        self.trace_recorder = trace_recorder or TraceRecorder()

    async def execute(self, dag: DAG) -> RunResult:
        self.trace_recorder = TraceRecorder()
        normalized = self.normalize(dag)
        validate_dag(normalized)
        self.apply_risk_overrides(normalized)
        self._enforce_review_gate(normalized)

        self.trace_recorder.record("dag_started", dag_id=normalized.dag_id)
        node_results: dict[str, NodeExecutionResult] = {}

        try:
            for batch in topo_batches(normalized):
                batch_results = await asyncio.gather(
                    *[
                        self.execute_node(node, normalized, node_results)
                        for node in batch
                    ]
                )
                for result in batch_results:
                    node_results[result.node_id] = result
        except Exception:
            self.trace_recorder.record("dag_failed", dag_id=normalized.dag_id)
            raise

        completed = all(result.completed for result in node_results.values())
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
        prompt = _node_prompt(node, completed_results)
        try:
            loop_result = await self.agent_loop.run(
                prompt,
                boundary=node.boundary,
                max_steps=node.max_steps,
                allowed_tools=node.tools,
            )
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

        if any(tool in medium_tools for tool in node.tools):
            risk = _max_risk(risk, "medium")
        if any(tool in high_tools for tool in node.tools):
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
