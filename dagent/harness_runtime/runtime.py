"""Top-level harness runtime.

The runtime owns the user-facing loop. It lets the top AgentLoop answer
directly, use ordinary runtime tools, or call the `dag_creator` control tool.
DAG nodes are executed by DAGExecutor through restricted child AgentLoops that
do not receive `dag_creator`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from dagent.harness_runtime.agent_loop import (
    AgentLoop,
    ControlToolResult,
    LoopEventHandler,
    TokenHandler,
)
from dagent.harness_runtime.control_plane import TaskRecord
from dagent.harness_runtime.dag_executor import DAGExecutionError, DAGExecutor, RunResult
from dagent.harness_runtime.dag_replanner import (
    DAGReplanner,
    NoOpDAGReplanner,
    ReplanContext,
    apply_replan_decision,
    replan_trace_event,
)
from dagent.harness_runtime.dag_validation import validate_dag
from dagent.harness_runtime.dag_creator import DagCreator
from dagent.harness_runtime.control_tools import DAG_CREATOR_NAME, dag_creator_tool_definition
from dagent.profiles import AgentProfile
from dagent.providers import ToolCall
from dagent.schemas import Boundary, DAG, NodeExecutionRecord, PermissionRequest
from dagent.state import PromptBuilder, PromptRequest
from dagent.tools.registry import Tool


RuntimeMode = Literal["auto", "direct", "dag_creator"]


@dataclass(frozen=True)
class HarnessMessageResult:
    status: Literal["completed", "awaiting_approval", "failed"]
    message_markdown: str
    dag: DAG | None = None
    run_result: RunResult | None = None
    task_id: str | None = None
    control_events: list[dict[str, Any]] = field(default_factory=list)


class HarnessRuntime:
    """Runs top-level messages and manages DAG lifecycle."""

    def __init__(
        self,
        *,
        agent_loop: AgentLoop,
        dag_creator: DagCreator,
        dag_executor: DAGExecutor,
        conversation_profile: AgentProfile,
        replanner: DAGReplanner | None = None,
        runtime_tools: list[Tool] | None = None,
        prompt_builder: PromptBuilder | None = None,
        auto_execute_approved_dags: bool = False,
        max_top_steps: int = 8,
        max_replans: int = 3,
    ) -> None:
        self.agent_loop = agent_loop
        self.dag_creator = dag_creator
        self.dag_executor = dag_executor
        self.replanner = replanner or NoOpDAGReplanner()
        self.conversation_profile = conversation_profile
        self.runtime_tools = runtime_tools or []
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.auto_execute_approved_dags = auto_execute_approved_dags
        self.max_top_steps = max_top_steps
        self.max_replans = max_replans
        self.tasks: dict[str, TaskRecord] = {}
        self.runs: dict[str, RunResult] = {}

    async def handle_message(
        self,
        message: str,
        *,
        mode: RuntimeMode = "auto",
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult:
        if mode == "dag_creator":
            record = await self.create_dag(message)
            return HarnessMessageResult(
                status="awaiting_approval" if record.dag.status == "review_required" else "completed",
                message_markdown=_dag_created_markdown(record),
                dag=record.dag,
                task_id=record.task_id,
                control_events=[_dag_event(record, "dag_created")],
            )

        messages = self.prompt_builder.build(
            PromptRequest(
                profile=self.conversation_profile,
                task_content="{{ user_message }}",
                tools=self.runtime_tools,
                memory=self.conversation_profile.memory,
                variables={"user_message": message},
            )
        )
        include_dag_creator = mode == "auto"
        result = await self.agent_loop.run(
            "",
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
            max_steps=self.max_top_steps,
            messages=messages,
            extra_tools=[dag_creator_tool_definition()] if include_dag_creator else None,
            control_tool_names={DAG_CREATOR_NAME} if include_dag_creator else None,
            control_tool_handler=self._handle_control_tool if include_dag_creator else None,
            on_token=on_token,
            on_event=on_event,
        )

        dag_event = _latest_dag_event(result.control_events)
        return HarnessMessageResult(
            status="awaiting_approval" if result.stop_reason == "awaiting_approval" else "completed",
            message_markdown=result.final_response,
            dag=dag_event.get("dag") if dag_event else None,
            run_result=dag_event.get("run_result") if dag_event else None,
            task_id=dag_event.get("task_id") if dag_event else None,
            control_events=result.control_events,
        )

    async def create_dag(self, request: str, *, task_id: str | None = None) -> TaskRecord:
        dag = await self.dag_creator.aplan(request, task_id=task_id)
        dag = self.prepare_dag_for_review(dag)
        record = TaskRecord(task_id=dag.task_id, user_request=request, dag=dag)
        self.tasks[record.task_id] = record
        return record

    def prepare_dag_for_review(self, dag: DAG) -> DAG:
        prepared = self.dag_executor.normalize(dag)
        validate_dag(prepared)
        self.dag_executor.apply_risk_overrides(prepared)
        prepared.status = self._initial_status(prepared)
        return prepared

    def approve_dag(self, task_id: str) -> DAG:
        record = self.tasks[task_id]
        record.dag.status = "approved"
        return record.dag

    async def execute_dag(self, task_id: str) -> RunResult:
        record = self.tasks[task_id]
        traces = []
        replan_count = 0
        try:
            while True:
                try:
                    result = await self.dag_executor.execute_next_ready_layer(
                        record.dag,
                        initial_results=_completed_results(record.node_results),
                        record_dag_start=not traces,
                    )
                except Exception as exc:
                    traces.extend(self.dag_executor.trace_recorder.events)
                    record.trace_records = self.dag_executor.trace_store.records_for_task(record.task_id)
                    decision = await self.replanner.replan(
                        ReplanContext(
                            task_id=record.task_id,
                            user_request=record.user_request,
                            dag=record.dag,
                            node_results=_completed_results(record.node_results),
                            trace_records=record.trace_records,
                            last_error=str(exc),
                            failed_node_id=_latest_failed_node_id(record.trace_records),
                        )
                    )
                    if decision.action != "replace" or decision.dag is None or replan_count >= self.max_replans:
                        raise
                    record.dag = self.prepare_dag_for_review(
                        apply_replan_decision(
                            current=record.dag,
                            decision=decision,
                            node_results=_completed_results(record.node_results),
                        )
                    )
                    traces.append(
                        replan_trace_event(
                            dag_id=record.dag.dag_id,
                            decision=decision,
                            applied=True,
                        )
                    )
                    replan_count += 1
                    if record.dag.status == "review_required":
                        result = RunResult(
                            dag_id=record.dag.dag_id,
                            completed=False,
                            node_results=dict(record.node_results),
                            traces=traces,
                        )
                        break
                    continue

                traces.extend(result.traces)
                record.node_results.update(result.node_results)
                record.trace_records = self.dag_executor.trace_store.records_for_task(record.task_id)
                if result.pending_permission_request is not None or result.completed:
                    break

                decision = await self.replanner.replan(
                    ReplanContext(
                        task_id=record.task_id,
                        user_request=record.user_request,
                        dag=record.dag,
                        node_results=_completed_results(record.node_results),
                        trace_records=record.trace_records,
                    )
                )
                if decision.action != "replace" or decision.dag is None:
                    continue
                record.dag = self.prepare_dag_for_review(
                    apply_replan_decision(
                        current=record.dag,
                        decision=decision,
                        node_results=_completed_results(record.node_results),
                    )
                )
                traces.append(
                    replan_trace_event(
                        dag_id=record.dag.dag_id,
                        decision=decision,
                        applied=True,
                    )
                )
                replan_count += 1
                if replan_count > self.max_replans:
                    raise RuntimeError("Maximum DAG replans exceeded.")
                if record.dag.status == "review_required":
                    result = RunResult(
                        dag_id=record.dag.dag_id,
                        completed=False,
                        node_results=dict(record.node_results),
                        traces=traces,
                    )
                    break

            result = RunResult(
                dag_id=record.dag.dag_id,
                completed=result.completed,
                node_results=result.node_results,
                traces=traces,
                pending_permission_request=result.pending_permission_request,
            )
        finally:
            record.trace_records = self.dag_executor.trace_store.records_for_task(record.task_id)
        record.node_results.update(result.node_results)
        record.runs.append(result)
        if result.pending_permission_request is not None:
            record.dag.status = "paused_for_permission"
            _node_by_id(record.dag, result.pending_permission_request.node_id).status = "blocked_permission"
        elif record.dag.status == "review_required":
            pass
        else:
            record.dag.status = "completed" if result.completed else "failed"
            for node_id, node_result in result.node_results.items():
                _node_by_id(record.dag, node_id).status = "completed" if node_result.completed else "failed"
        self.runs[f"run_{uuid4().hex}"] = result
        return result

    def approve_permission(
        self,
        task_id: str,
        *,
        boundary: Boundary | None = None,
    ) -> PermissionRequest:
        record = self.tasks[task_id]
        if not record.runs or record.runs[-1].pending_permission_request is None:
            raise KeyError("No pending permission request.")
        request = record.runs[-1].pending_permission_request
        node = _node_by_id(record.dag, request.node_id)
        node.boundary = boundary or request.requested_boundary
        node.status = "ready"
        request.status = "approved"
        record.dag.status = "approved"
        return request

    def deny_permission(self, task_id: str) -> PermissionRequest:
        record = self.tasks[task_id]
        if not record.runs or record.runs[-1].pending_permission_request is None:
            raise KeyError("No pending permission request.")
        request = record.runs[-1].pending_permission_request
        request.status = "denied"
        _node_by_id(record.dag, request.node_id).status = "failed"
        record.dag.status = "aborted"
        return request

    async def _handle_control_tool(self, tool_call: ToolCall) -> ControlToolResult:
        if tool_call.name != DAG_CREATOR_NAME:
            raise ValueError(f"Unsupported control tool '{tool_call.name}'.")

        request = str(tool_call.arguments.get("request") or "").strip()
        reason = str(tool_call.arguments.get("reason") or "").strip()
        if not request:
            request = "Create a reviewable DAG for the current user task."

        record = await self.create_dag(request)
        event = _dag_event(record, "dag_created", reason=reason)
        if record.dag.status == "review_required":
            return ControlToolResult(
                content=_dag_created_tool_output(record, reason=reason),
                stop_reason="awaiting_approval",
                events=[event],
            )

        if not self.auto_execute_approved_dags:
            return ControlToolResult(
                content=_dag_created_tool_output(record, reason=reason),
                stop_reason="awaiting_approval",
                events=[event],
            )

        try:
            result = await self.execute_dag(record.task_id)
        except DAGExecutionError as exc:
            record.dag.status = "review_required"
            return ControlToolResult(
                content=f"DAG requires approval before execution: {exc}",
                stop_reason="awaiting_approval",
                events=[event],
            )

        event["kind"] = "dag_executed"
        event["run_result"] = result
        return ControlToolResult(
            content=_dag_run_tool_output(record, result),
            events=[event],
        )

    def _initial_status(self, dag: DAG) -> str:
        needs_review = any(node.risk in {"medium", "high"} for node in dag.nodes)
        return "review_required" if needs_review else "approved"


def _dag_event(
    record: TaskRecord,
    kind: str,
    *,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "task_id": record.task_id,
        "dag": record.dag,
        "reason": reason,
    }


def _latest_dag_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("kind") in {"dag_created", "dag_executed"}:
            return event
    return None


def _dag_created_markdown(record: TaskRecord) -> str:
    return "\n".join(
        [
            "### DAG created",
            f"- **Task:** `{record.task_id}`",
            f"- **Status:** `{record.dag.status}`",
            f"- **Nodes:** {len(record.dag.nodes)}",
        ]
    )


def _dag_created_tool_output(record: TaskRecord, *, reason: str) -> str:
    return json.dumps(
        {
            "status": record.dag.status,
            "task_id": record.task_id,
            "reason": reason,
            "dag": record.dag.model_dump(mode="json"),
        },
        ensure_ascii=False,
    )


def _dag_run_tool_output(record: TaskRecord, result: RunResult) -> str:
    return json.dumps(
        {
            "status": "completed" if result.completed else "failed",
            "task_id": record.task_id,
            "dag_id": result.dag_id,
            "node_results": {
                node_id: {
                    "completed": node_result.completed,
                    "stop_reason": node_result.stop_reason,
                    "final_response": node_result.final_response,
                }
                for node_id, node_result in result.node_results.items()
            },
        },
        ensure_ascii=False,
    )


def _completed_results(node_results: dict) -> dict:
    return {
        node_id: result
        for node_id, result in node_results.items()
        if getattr(result, "completed", False)
    }


def _node_by_id(dag: DAG, node_id: str):
    for node in dag.nodes:
        if node.id == node_id:
            return node
    raise KeyError(node_id)


def _latest_failed_node_id(records: list[NodeExecutionRecord]) -> str | None:
    for record in reversed(records):
        if record.status == "failed":
            return record.node_id
    return None
