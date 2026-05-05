"""Local DAG replanning after node observations."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import uuid4

from dagent.harness_runtime.dag_creator import dag_from_json_payload
from dagent.harness_runtime.dag_executor import NodeExecutionResult
from dagent.harness_runtime.dag_validation import validate_dag
from dagent.harness_runtime.profiled_agent import extract_json_object
from dagent.profiles import AgentProfile, ProfileStore
from dagent.providers import ChatProvider
from dagent.schemas import Boundary, DAG, DAGEdge, NodeExecutionRecord, TraceEvent
from dagent.state import PromptBuilder, PromptRequest
from dagent.tools.registry import Tool


ReplanAction = Literal["keep", "patch_node", "replace", "abort"]


@dataclass(frozen=True)
class ReplanContext:
    task_id: str
    user_request: str
    dag: DAG
    node_results: dict[str, NodeExecutionResult] = field(default_factory=dict)
    trace_records: list[NodeExecutionRecord] = field(default_factory=list)
    last_error: str | None = None
    failed_node_id: str | None = None


@dataclass(frozen=True)
class ReplanDecision:
    action: ReplanAction
    reason: str = ""
    dag: DAG | None = None
    node_id: str | None = None
    tool: str | None = None
    args: dict[str, Any] | None = None
    boundary: Boundary | None = None


class DAGReplanner(Protocol):
    async def replan(self, context: ReplanContext) -> ReplanDecision:
        """Decide whether to keep or replace the unfinished DAG portion."""


class NoOpDAGReplanner:
    async def replan(self, context: ReplanContext) -> ReplanDecision:
        return ReplanDecision(action="keep", reason="No replanner configured.")


class LLMLocalDAGReplanner:
    """LLM-backed replanner that may replace only unfinished DAG nodes."""

    def __init__(
        self,
        provider: ChatProvider,
        *,
        profile: AgentProfile | None = None,
        profile_store: ProfileStore | None = None,
        profile_name: str = "dag_replanner",
        prompt_builder: PromptBuilder | None = None,
        tools: list[Tool] | None = None,
    ) -> None:
        self.provider = provider
        self.profile = profile or (profile_store or ProfileStore()).load(profile_name)
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.tools = tools or []

    async def replan(self, context: ReplanContext) -> ReplanDecision:
        response = await self.provider.chat(
            self.prompt_builder.build(
                PromptRequest(
                    profile=self.profile,
                    task_content=(
                        "Task id: {{ task_id }}\n"
                        "User request:\n{{ user_request }}\n\n"
                        "Current DAG JSON:\n{{ dag_json }}\n\n"
                        "Completed and failed node records:\n{{ trace_json }}\n\n"
                        "Completed node result ids: {{ completed_ids }}\n"
                        "Failed node id: {{ failed_node_id }}\n"
                        "Last error: {{ last_error }}\n\n"
                        "Return the local replan decision JSON now."
                    ),
                    tools=self.tools,
                    memory=self.profile.memory,
                    variables={
                        "task_id": context.task_id,
                        "user_request": context.user_request,
                        "dag_json": json.dumps(
                            context.dag.model_dump(mode="json"),
                            ensure_ascii=False,
                        ),
                        "trace_json": json.dumps(
                            [
                                record.model_dump(mode="json")
                                for record in context.trace_records
                            ],
                            ensure_ascii=False,
                        ),
                        "completed_ids": ", ".join(sorted(_completed_ids(context))),
                        "failed_node_id": context.failed_node_id or "",
                        "last_error": context.last_error or "",
                    },
                )
            )
        )
        payload = extract_json_object(response.content)
        action = str(payload.get("action") or "keep").strip().lower()
        reason = str(payload.get("reason") or "")
        if action == "abort":
            return ReplanDecision(action="abort", reason=reason)

        if action == "patch_node":
            return ReplanDecision(
                action="patch_node",
                reason=reason,
                node_id=str(payload.get("node_id") or context.failed_node_id or ""),
                tool=_optional_str(payload.get("tool")),
                args=payload.get("args") if isinstance(payload.get("args"), dict) else None,
                boundary=(
                    Boundary.model_validate(payload["boundary"])
                    if isinstance(payload.get("boundary"), dict)
                    else None
                ),
            )

        if action != "replace":
            return ReplanDecision(action="keep", reason=reason)

        replacement_payload = payload.get("dag") or payload.get("plan")
        if not isinstance(replacement_payload, dict):
            return ReplanDecision(
                action="keep",
                reason=reason or "Replanner requested replacement without dag or plan.",
            )
        replacement = dag_from_json_payload(
            replacement_payload,
            task_id=context.task_id,
        )
        return ReplanDecision(action="replace", reason=reason, dag=replacement)


def apply_replan_decision(
    *,
    current: DAG,
    decision: ReplanDecision,
    node_results: dict[str, NodeExecutionResult],
) -> DAG:
    if decision.action != "replace" or decision.dag is None:
        return current

    locked_ids = _completed_result_ids(node_results)
    locked_nodes = [
        node.model_copy(deep=True)
        for node in current.nodes
        if node.id in locked_ids
    ]
    for node in locked_nodes:
        node.status = "completed"
    replacement_nodes = [
        node.model_copy(deep=True)
        for node in decision.dag.nodes
        if node.id not in locked_ids
    ]
    replacement_ids = {node.id for node in replacement_nodes}
    if len(replacement_ids) != len(replacement_nodes):
        raise ValueError("Replanned DAG contains duplicate unfinished node IDs.")
    merged_ids = locked_ids | replacement_ids

    locked_edges = [
        edge.model_copy(deep=True)
        for edge in current.edges
        if edge.source in locked_ids and edge.target in locked_ids
    ]
    replacement_edges: list[DAGEdge] = []
    for edge in decision.dag.edges:
        if edge.target in locked_ids:
            raise ValueError(
                f"Replanned DAG cannot replace dependency target '{edge.target}' "
                "because it is already completed."
            )
        if edge.source not in merged_ids or edge.target not in merged_ids:
            raise ValueError(
                f"Replanned edge '{edge.source}' -> '{edge.target}' references an unknown node."
            )
        replacement_edges.append(edge.model_copy(deep=True))

    merged = current.model_copy(deep=True)
    merged.version = current.version + 1
    merged.status = "draft"
    merged.nodes = [*locked_nodes, *replacement_nodes]
    merged.edges = [*locked_edges, *replacement_edges]
    validate_dag(merged)
    return merged


def apply_node_patch_decision(
    *,
    current: DAG,
    decision: ReplanDecision,
    completed_node_results: dict[str, NodeExecutionResult],
) -> DAG:
    if decision.action != "patch_node":
        return current
    if not decision.node_id:
        raise ValueError("patch_node decision requires node_id.")

    patched = current.model_copy(deep=True)
    node = _node_by_id(patched, decision.node_id)
    if decision.tool:
        node.tool = decision.tool
        if decision.tool not in node.tools:
            node.tools = [decision.tool, *node.tools]
    if decision.args is not None:
        node.args = dict(decision.args)
    if decision.boundary is not None:
        node.boundary = decision.boundary
    node.kind = "tool"
    if node.tool and node.tool not in node.tools:
        node.tools = [node.tool, *node.tools]
    node.max_steps = 1
    node.status = "ready"
    patched.version = current.version + 1
    patched.status = "draft"
    validate_dag(patched)
    return patched


def affected_node_ids_for_patch(dag: DAG, node_id: str) -> set[str]:
    """Return the patched node plus all downstream nodes that must be rerun."""
    _node_by_id(dag, node_id)
    affected = {node_id}
    changed = True
    while changed:
        changed = False
        for edge in dag.edges:
            if edge.source in affected and edge.target not in affected:
                affected.add(edge.target)
                changed = True
    return affected


def replan_trace_event(
    *,
    dag_id: str,
    decision: ReplanDecision,
    applied: bool,
) -> TraceEvent:
    if applied:
        event_type = "node_patched" if decision.action == "patch_node" else "dag_replanned"
    elif decision.action == "keep":
        event_type = "dag_replan_kept"
    elif decision.action == "abort":
        event_type = "dag_aborted"
    else:
        event_type = "dag_replan_failed"
    return TraceEvent(
        event_id=f"trace_{uuid4().hex}",
        event_type=event_type,
        dag_id=dag_id,
        payload={
            "action": decision.action,
            "reason": decision.reason,
            "node_id": decision.node_id,
            "tool": decision.tool,
            "args": decision.args,
        },
    )


def _completed_ids(context: ReplanContext) -> set[str]:
    return _completed_result_ids(context.node_results)


def _completed_result_ids(
    node_results: dict[str, NodeExecutionResult],
) -> set[str]:
    return {
        node_id
        for node_id, result in node_results.items()
        if result.completed
    }


def _node_by_id(dag: DAG, node_id: str):
    for node in dag.nodes:
        if node.id == node_id:
            return node
    raise ValueError(f"Node '{node_id}' not found.")


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
