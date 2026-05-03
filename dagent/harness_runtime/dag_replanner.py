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
from dagent.schemas import DAG, DAGEdge, NodeExecutionRecord, TraceEvent
from dagent.state import PromptBuilder, PromptRequest
from dagent.tools.registry import Tool


ReplanAction = Literal["keep", "replace"]


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


def replan_trace_event(
    *,
    dag_id: str,
    decision: ReplanDecision,
    applied: bool,
) -> TraceEvent:
    if applied:
        event_type = "dag_replanned"
    elif decision.action == "keep":
        event_type = "dag_replan_kept"
    else:
        event_type = "dag_replan_failed"
    return TraceEvent(
        event_id=f"trace_{uuid4().hex}",
        event_type=event_type,
        dag_id=dag_id,
        payload={
            "action": decision.action,
            "reason": decision.reason,
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
