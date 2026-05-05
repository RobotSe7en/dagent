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
from dagent.harness_runtime.dag_executor import (
    DAGExecutionError,
    DAGExecutor,
    RunResult,
    _inject_placeholders,
    _next_ready_nodes,
)
from dagent.harness_runtime.dag_replanner import (
    DAGReplanner,
    NoOpDAGReplanner,
    ReplanContext,
    affected_node_ids_for_patch,
    apply_node_patch_decision,
    apply_replan_decision,
    replan_trace_event,
)
from dagent.harness_runtime.dag_validation import validate_dag
from dagent.harness_runtime.dag_creator import DagCreator
from dagent.harness_runtime.control_tools import DAG_CREATOR_NAME, dag_creator_tool_definition
from dagent.harness_runtime.review_policy import ReviewLevel, ReviewPolicy, review_policy
from dagent.harness_runtime.task_record import PendingReview, TaskRecord
from dagent.profiles import AgentProfile
from dagent.providers import ToolCall
from dagent.schemas import Boundary, DAG, DAGNode, NodeExecutionRecord, PermissionRequest, TraceEvent
from dagent.state import PromptBuilder, PromptRequest
from dagent.tools.registry import Tool


RuntimeMode = Literal["auto", "direct", "dag"]


@dataclass(frozen=True)
class HarnessMessageResult:
    status: Literal["completed", "awaiting_dag_review", "awaiting_change_review", "awaiting_approval", "failed"]
    message_markdown: str
    dag: DAG | None = None
    run_result: RunResult | None = None
    task_id: str | None = None
    control_events: list[dict[str, Any]] = field(default_factory=list)
    pending_review: PendingReview | None = None


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
        auto_execute_approved_dags: bool = True,
        max_top_steps: int = 8,
        max_replans: int = 3,
        max_node_retries: int = 2,
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
        self.max_node_retries = max_node_retries
        self.tasks: dict[str, TaskRecord] = {}
        self.runs: dict[str, RunResult] = {}
        self._active_review_level: ReviewLevel = "balanced"

    async def handle_message(
        self,
        message: str,
        *,
        mode: RuntimeMode = "auto",
        review_level: ReviewLevel = "balanced",
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult:
        if mode == "dag":
            record = await self.create_dag(message, review_level=review_level)
            if not review_policy(review_level).requires_initial_dag_review(record.dag):
                record.dag.status = "approved"
                result = await self.execute_dag(record.task_id)
                summary = await self._summarize_dag_run(record, result, on_token=on_token, on_event=on_event)
                return HarnessMessageResult(
                    status="completed" if result.completed else "failed",
                    message_markdown=summary,
                    dag=record.dag,
                    run_result=result,
                    task_id=record.task_id,
                )
            return HarnessMessageResult(
                status="awaiting_dag_review",
                message_markdown=_dag_created_review_message(record),
                dag=record.dag,
                task_id=record.task_id,
                control_events=[_dag_event(record, "dag_created", reason="Forced DAG mode.")],
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
        self._active_review_level = review_level
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
            status="awaiting_dag_review" if result.stop_reason == "awaiting_approval" else "completed",
            message_markdown=result.final_response,
            dag=dag_event.get("dag") if dag_event else None,
            run_result=dag_event.get("run_result") if dag_event else None,
            task_id=dag_event.get("task_id") if dag_event else None,
            control_events=result.control_events,
            pending_review=dag_event.get("pending_review") if dag_event else None,
        )

    async def resume_dag(
        self,
        task_id: str,
        dag: DAG,
        review_level: ReviewLevel | None = None,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult:
        if task_id not in self.tasks:
            raise KeyError(f"Unknown task '{task_id}'.")

        record = self.tasks[task_id]
        if review_level is not None:
            record.review_level = review_level
        submitted = dag.model_copy(deep=True)
        submitted.task_id = task_id
        submitted.dag_id = record.dag.dag_id
        submitted.version = record.dag.version + 1
        existing_node_ids = {node.id for node in record.dag.nodes}
        for node_id in _changed_node_ids(record.dag, submitted):
            if node_id in existing_node_ids:
                _invalidate_patch_results(record, node_id)
        record.dag = self.prepare_dag_for_review(submitted)
        record.dag.status = "approved"
        record.pending_review = None
        record.suppress_next_review = True

        result = await self.execute_dag(task_id)
        if record.pending_review is not None:
            return HarnessMessageResult(
                status="awaiting_change_review",
                message_markdown=record.pending_review.message,
                dag=record.pending_review.proposed_dag,
                run_result=result,
                task_id=task_id,
                pending_review=record.pending_review,
                control_events=[_dag_event(record, "change_review_requested", reason=record.pending_review.message)],
            )
        summary = await self._summarize_dag_run(record, result, on_token=on_token, on_event=on_event)
        return HarnessMessageResult(
            status="completed" if result.completed else "failed",
            message_markdown=summary,
            dag=record.dag,
            run_result=result,
            task_id=task_id,
            control_events=[_dag_event(record, "dag_executed", reason="User confirmed DAG.")],
        )

    async def create_dag(
        self,
        request: str,
        *,
        task_id: str | None = None,
        review_level: ReviewLevel = "balanced",
    ) -> TaskRecord:
        dag = await self.dag_creator.aplan(request, task_id=task_id)
        dag = self.prepare_dag_for_review(dag)
        record = TaskRecord(task_id=dag.task_id, user_request=request, dag=dag, review_level=review_level)
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
        repair_counts: dict[str, int] = {}
        try:
            while True:
                review_result = self._maybe_pause_for_arg_injection_or_node_execution(record, traces)
                if review_result is not None:
                    result = review_result
                    break
                try:
                    result = await self.dag_executor.execute_next_ready_layer(
                        record.dag,
                        initial_results=_completed_results(record.node_results),
                        record_dag_start=not traces,
                    )
                except Exception as exc:
                    traces.extend(self.dag_executor.trace_recorder.events)
                    record.trace_records = self.dag_executor.trace_store.records_for_task(record.task_id)
                    failed_node_id = _latest_failed_node_id(record.trace_records)
                    if failed_node_id:
                        _node_by_id(record.dag, failed_node_id).status = "failed"
                    decision = await self.replanner.replan(
                        ReplanContext(
                            task_id=record.task_id,
                            user_request=record.user_request,
                            dag=record.dag,
                            node_results=_completed_results(record.node_results),
                            trace_records=record.trace_records,
                            last_error=str(exc),
                            failed_node_id=failed_node_id,
                        )
                    )
                    if decision.action == "abort":
                        traces.append(
                            replan_trace_event(
                                dag_id=record.dag.dag_id,
                                decision=decision,
                                applied=False,
                            )
                        )
                        result = RunResult(
                            dag_id=record.dag.dag_id,
                            completed=False,
                            node_results=dict(record.node_results),
                            traces=traces,
                        )
                        record.dag.status = "aborted"
                        break
                    if decision.action == "patch_node":
                        node_id = decision.node_id or _latest_failed_node_id(record.trace_records)
                        if not node_id:
                            raise
                        repair_counts[node_id] = repair_counts.get(node_id, 0) + 1
                        if repair_counts[node_id] > self.max_node_retries:
                            raise RuntimeError(
                                f"Maximum repair retries exceeded for node '{node_id}'."
                            ) from exc
                        pending = self._maybe_create_replan_review(record, decision)
                        if pending is not None:
                            traces.append(_review_trace(record.dag.dag_id, pending))
                            result = RunResult(
                                dag_id=record.dag.dag_id,
                                completed=False,
                                node_results=dict(record.node_results),
                                traces=traces,
                            )
                            break
                        _invalidate_patch_results(record, node_id)
                        record.dag = self.prepare_dag_for_review(
                            apply_node_patch_decision(
                                current=record.dag,
                                decision=decision,
                                completed_node_results=_completed_results(record.node_results),
                            )
                        )
                        traces.append(
                            replan_trace_event(
                                dag_id=record.dag.dag_id,
                                decision=decision,
                                applied=True,
                            )
                        )
                        continue
                    if decision.action != "replace" or decision.dag is None or replan_count >= self.max_replans:
                        raise
                    pending = self._maybe_create_replan_review(record, decision)
                    if pending is not None:
                        traces.append(_review_trace(record.dag.dag_id, pending))
                        result = RunResult(
                            dag_id=record.dag.dag_id,
                            completed=False,
                            node_results=dict(record.node_results),
                            traces=traces,
                        )
                        break
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
                if decision.action == "abort":
                    traces.append(
                        replan_trace_event(
                            dag_id=record.dag.dag_id,
                            decision=decision,
                            applied=False,
                        )
                    )
                    result = RunResult(
                        dag_id=record.dag.dag_id,
                        completed=False,
                        node_results=dict(record.node_results),
                        traces=traces,
                    )
                    record.dag.status = "aborted"
                    break
                if decision.action == "patch_node":
                    node_id = decision.node_id or _latest_failed_node_id(record.trace_records)
                    if not node_id:
                        raise RuntimeError("patch_node decision requires node_id.")
                    repair_counts[node_id] = repair_counts.get(node_id, 0) + 1
                    if repair_counts[node_id] > self.max_node_retries:
                        raise RuntimeError(
                            f"Maximum repair retries exceeded for node '{node_id}'."
                        )
                    pending = self._maybe_create_replan_review(record, decision)
                    if pending is not None:
                        traces.append(_review_trace(record.dag.dag_id, pending))
                        result = RunResult(
                            dag_id=record.dag.dag_id,
                            completed=False,
                            node_results=dict(record.node_results),
                            traces=traces,
                        )
                        break
                    _invalidate_patch_results(record, node_id)
                    record.dag = self.prepare_dag_for_review(
                        apply_node_patch_decision(
                            current=record.dag,
                            decision=decision,
                            completed_node_results=_completed_results(record.node_results),
                        )
                    )
                    traces.append(
                        replan_trace_event(
                            dag_id=record.dag.dag_id,
                            decision=decision,
                            applied=True,
                        )
                    )
                    continue
                if decision.action != "replace" or decision.dag is None:
                    continue
                pending = self._maybe_create_replan_review(record, decision)
                if pending is not None:
                    traces.append(_review_trace(record.dag.dag_id, pending))
                    result = RunResult(
                        dag_id=record.dag.dag_id,
                        completed=False,
                        node_results=dict(record.node_results),
                        traces=traces,
                    )
                    break
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
        if record.pending_review is not None:
            record.dag.status = "paused_for_replan"
        elif result.pending_permission_request is not None:
            record.pending_permission_request = result.pending_permission_request
            record.dag.status = "paused_for_permission"
            _node_by_id(record.dag, result.pending_permission_request.node_id).status = "blocked_permission"
        elif record.dag.status == "review_required":
            pass
        elif record.dag.status == "aborted":
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
        record.pending_permission_request = None
        record.dag.status = "approved"
        return request

    def deny_permission(self, task_id: str) -> PermissionRequest:
        record = self.tasks[task_id]
        if not record.runs or record.runs[-1].pending_permission_request is None:
            raise KeyError("No pending permission request.")
        request = record.runs[-1].pending_permission_request
        request.status = "denied"
        record.pending_permission_request = None
        _node_by_id(record.dag, request.node_id).status = "failed"
        record.dag.status = "aborted"
        return request

    def _maybe_pause_for_arg_injection_or_node_execution(
        self,
        record: TaskRecord,
        traces: list[TraceEvent],
    ) -> RunResult | None:
        if record.suppress_next_review:
            record.suppress_next_review = False
            return None

        policy = review_policy(record.review_level)
        ready_nodes = _next_ready_nodes(record.dag, _completed_results(record.node_results))
        if not ready_nodes:
            return None

        proposed = record.dag.model_copy(deep=True)
        proposed_nodes = {node.id: node for node in proposed.nodes}
        injected_nodes: list[str] = []
        for node in ready_nodes:
            proposed_node = proposed_nodes[node.id]
            injected_args = _inject_placeholders(proposed_node.args, _completed_results(record.node_results))
            if injected_args != proposed_node.args:
                proposed_node.args = injected_args
                injected_nodes.append(node.id)

        pending: PendingReview | None = None
        if injected_nodes and policy.requires_arg_injection_review():
            pending = PendingReview(
                review_id=f"review_{uuid4().hex}",
                kind="arg_injection",
                message=f"Review injected args for node(s): {', '.join(injected_nodes)}.",
                proposed_dag=proposed,
                payload={"node_ids": injected_nodes},
            )
        elif policy.requires_node_execution_review():
            pending = PendingReview(
                review_id=f"review_{uuid4().hex}",
                kind="node_execution",
                message=f"Review next node execution layer: {', '.join(node.id for node in ready_nodes)}.",
                proposed_dag=proposed,
                payload={"node_ids": [node.id for node in ready_nodes]},
            )

        if pending is None:
            return None

        record.pending_review = pending
        record.dag = proposed
        traces.append(_review_trace(record.dag.dag_id, pending))
        return RunResult(
            dag_id=record.dag.dag_id,
            completed=False,
            node_results=dict(record.node_results),
            traces=traces,
        )

    def _maybe_create_replan_review(
        self,
        record: TaskRecord,
        decision: Any,
    ) -> PendingReview | None:
        if record.suppress_next_review:
            record.suppress_next_review = False
            return None
        policy = review_policy(record.review_level)
        if not policy.requires_replan_review(decision, current=record.dag):
            return None

        if decision.action == "patch_node":
            proposed = apply_node_patch_decision(
                current=record.dag,
                decision=decision,
                completed_node_results=_completed_results(record.node_results),
            )
            kind = "node_patch"
            message = f"Review proposed patch for node '{decision.node_id}'."
        elif decision.action == "replace" and decision.dag is not None:
            proposed = apply_replan_decision(
                current=record.dag,
                decision=decision,
                node_results=_completed_results(record.node_results),
            )
            kind = "dag_replan"
            message = "Review proposed local DAG replan."
        else:
            return None

        proposed = self.prepare_dag_for_review(proposed)
        pending = PendingReview(
            review_id=f"review_{uuid4().hex}",
            kind=kind,
            message=message,
            proposed_dag=proposed,
            decision=decision,
            payload={"reason": decision.reason},
        )
        record.pending_review = pending
        record.dag = proposed
        return pending

    async def _handle_control_tool(self, tool_call: ToolCall) -> ControlToolResult:
        if tool_call.name != DAG_CREATOR_NAME:
            raise ValueError(f"Unsupported control tool '{tool_call.name}'.")

        request = str(tool_call.arguments.get("request") or "").strip()
        reason = str(tool_call.arguments.get("reason") or "").strip()
        if not request:
            request = "Create a reviewable DAG for the current user task."

        record = await self.create_dag(request, review_level=self._active_review_level)
        event = _dag_event(record, "dag_created", reason=reason)
        if record.dag.status == "review_required":
            return ControlToolResult(
                content=_dag_created_tool_output(record, reason=reason),
                stop_reason="awaiting_approval",
                events=[event],
            )

        return ControlToolResult(
            content=_dag_created_tool_output(record, reason=reason),
            stop_reason="awaiting_approval",
            events=[event],
        )

    async def _summarize_dag_run(
        self,
        record: TaskRecord,
        result: RunResult,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> str:
        messages = self.prompt_builder.build(
            PromptRequest(
                profile=self.conversation_profile,
                task_content="{{ user_message }}",
                tools=self.runtime_tools,
                memory=self.conversation_profile.memory,
                variables={
                    "user_message": (
                        "The user confirmed a DAG and it has been executed.\n"
                        f"Original request:\n{record.user_request}\n\n"
                        f"DAG execution observation:\n{_dag_run_tool_output(record, result)}\n\n"
                        "Answer the user's original request directly."
                    )
                },
            )
        )
        stream_kwargs: dict[str, Any] = {}
        if on_token is not None:
            stream_kwargs["on_token"] = on_token
        if on_event is not None:
            stream_kwargs["on_event"] = on_event
        summary = await self.agent_loop.run(
            "",
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
            max_steps=self.max_top_steps,
            messages=messages,
            **stream_kwargs,
        )
        return summary.final_response

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


def _dag_created_review_message(record: TaskRecord) -> str:
    return "\n".join(
        [
            "### DAG ready for review",
            f"- **Task:** `{record.task_id}`",
            f"- **Status:** `{record.dag.status}`",
            f"- **Nodes:** {len(record.dag.nodes)}",
            "- **Next action:** Review and edit the DAG, then confirm to resume execution.",
        ]
    )


def _review_trace(dag_id: str, pending: PendingReview) -> TraceEvent:
    return TraceEvent(
        event_id=f"trace_{uuid4().hex}",
        event_type="review_requested",
        dag_id=dag_id,
        payload={
            "review_id": pending.review_id,
            "kind": pending.kind,
            "message": pending.message,
            **pending.payload,
        },
    )


def _dag_run_tool_output(record: TaskRecord, result: RunResult) -> str:
    return json.dumps(
        {
            "status": "completed" if result.completed else "failed",
            "task_id": record.task_id,
            "dag_id": result.dag_id,
            "user_request": record.user_request,
            "instruction": (
                "Summarize the DAG execution result and answer the user's original "
                "request directly. Use the node_results as observations."
            ),
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


def _invalidate_patch_results(record: TaskRecord, node_id: str) -> None:
    for affected_id in affected_node_ids_for_patch(record.dag, node_id):
        record.node_results.pop(affected_id, None)
        try:
            _node_by_id(record.dag, affected_id).status = "ready"
        except KeyError:
            continue


def _changed_node_ids(current: DAG, proposed: DAG) -> set[str]:
    current_nodes = {node.id: node for node in current.nodes}
    proposed_nodes = {node.id: node for node in proposed.nodes}
    changed = set(current_nodes) - set(proposed_nodes)
    changed.update(set(proposed_nodes) - set(current_nodes))
    for node_id, proposed_node in proposed_nodes.items():
        current_node = current_nodes.get(node_id)
        if current_node is None:
            changed.add(node_id)
            continue
        if current_node.model_dump(mode="json") != proposed_node.model_dump(mode="json"):
            changed.add(node_id)
    if current.edges != proposed.edges:
        changed.update(proposed_nodes)
    return changed
