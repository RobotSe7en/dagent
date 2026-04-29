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

from dagent.harness_runtime.agent_loop import AgentLoop, ControlToolResult
from dagent.harness_runtime.control_plane import TaskRecord
from dagent.harness_runtime.dag_executor import DAGExecutionError, DAGExecutor, RunResult
from dagent.harness_runtime.dag_validation import validate_dag
from dagent.harness_runtime.dag_creator import DagCreator
from dagent.harness_runtime.control_tools import DAG_CREATOR_NAME, dag_creator_tool_definition
from dagent.profiles import AgentProfile
from dagent.providers import ToolCall
from dagent.schemas import Boundary, DAG
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
        planner: DagCreator,
        dag_executor: DAGExecutor,
        conversation_profile: AgentProfile,
        runtime_tools: list[Tool] | None = None,
        prompt_builder: PromptBuilder | None = None,
        auto_execute_approved_dags: bool = True,
        max_top_steps: int = 8,
    ) -> None:
        self.agent_loop = agent_loop
        self.planner = planner
        self.dag_executor = dag_executor
        self.conversation_profile = conversation_profile
        self.runtime_tools = runtime_tools or []
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.auto_execute_approved_dags = auto_execute_approved_dags
        self.max_top_steps = max_top_steps
        self.tasks: dict[str, TaskRecord] = {}
        self.runs: dict[str, RunResult] = {}

    async def handle_message(
        self,
        message: str,
        *,
        mode: RuntimeMode = "auto",
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
        dag = await self.planner.aplan(request, task_id=task_id)
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
        result = await self.dag_executor.execute(record.dag)
        record.runs.append(result)
        record.dag.status = "completed" if result.completed else "failed"
        self.runs[f"run_{uuid4().hex}"] = result
        return result

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
