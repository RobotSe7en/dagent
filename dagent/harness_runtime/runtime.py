"""Top-level harness runtime.

The runtime owns the user-facing loop. It lets the top AgentLoop answer
directly, use ordinary runtime tools, or call the `dag_agent` control tool.
DAG nodes are executed by DAGExecutor through restricted child AgentLoops that
do not receive `dag_agent`.
"""

from __future__ import annotations

import asyncio
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
from dagent.harness_runtime.dag_validation import validate_dag
from dagent.harness_runtime.dag_agent import DAGAgent
from dagent.harness_runtime.control_tools import DAG_AGENT_NAME, dag_agent_tool_definition
from dagent.harness_runtime.review_policy import ReviewLevel, review_policy
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


@dataclass(frozen=True)
class _DAGRevisionApplyResult:
    reason: str
    changed_node_ids: set[str]
    pending_review: PendingReview | None = None


class HarnessRuntime:
    """Runs top-level messages and manages DAG lifecycle."""

    def __init__(
        self,
        *,
        agent_loop: AgentLoop,
        dag_agent: DAGAgent,
        dag_executor: DAGExecutor,
        conversation_profile: AgentProfile,
        runtime_tools: list[Tool] | None = None,
        prompt_builder: PromptBuilder | None = None,
        auto_execute_approved_dags: bool = True,
        max_top_steps: int = 8,
        max_replans: int = 3,
        max_node_retries: int = 2,
    ) -> None:
        self.agent_loop = agent_loop
        self.dag_agent = dag_agent
        self.dag_executor = dag_executor
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
        self._active_user_message: str = ""
        self._active_continuation_task_id: str | None = None

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
            record = await self.create_dag(
                message,
                review_level=review_level,
                planning_context=self._conversation_context(),
                runtime_mode="dag",
            )
            if not review_policy(review_level).requires_initial_dag_review(record.dag):
                record.dag.status = "approved"
                result = await self.execute_dag(record.task_id)
                if record.pending_review is not None:
                    return HarnessMessageResult(
                        status="awaiting_change_review",
                        message_markdown=record.pending_review.message,
                        dag=record.pending_review.proposed_dag,
                        run_result=result,
                        task_id=record.task_id,
                        pending_review=record.pending_review,
                        control_events=[
                            _dag_event(
                                record,
                                "change_review_requested",
                                reason=record.pending_review.message,
                            )
                        ],
                    )
                summary = await self._summarize_dag_run(record, result, on_token=on_token, on_event=on_event)
                return HarnessMessageResult(
                    status="completed" if result.completed else "failed",
                    message_markdown=summary,
                    dag=record.dag,
                    run_result=result,
                    task_id=record.task_id,
                )
            record.dag.status = "review_required"
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
                context=self._conversation_context(),
                variables={"user_message": message},
            )
        )
        include_dag_agent = mode == "auto"
        self._active_review_level = review_level
        self._active_user_message = message
        try:
            result = await self.agent_loop.run(
                "",
                boundary=Boundary(mode="read_only", allowed_paths=["."]),
                max_steps=self.max_top_steps,
                messages=messages,
                extra_tools=[dag_agent_tool_definition()] if include_dag_agent else None,
                control_tool_names={DAG_AGENT_NAME} if include_dag_agent else None,
                control_tool_handler=self._handle_control_tool if include_dag_agent else None,
                on_token=on_token,
                on_event=on_event,
            )
        finally:
            self._active_user_message = ""

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
        prepared = self.prepare_dag_for_review(submitted)

        existing_node_ids = {node.id for node in record.dag.nodes}
        changed_node_ids = _changed_node_ids(record.dag, prepared)
        if (
            record.pending_review is not None
            and record.pending_review.kind == "execution_error"
            and not changed_node_ids
        ):
            record.dag.status = _pending_review_dag_status(record.pending_review)
            return HarnessMessageResult(
                status="awaiting_change_review",
                message_markdown=(
                    "The failed DAG was confirmed without any node or edge changes. "
                    "Edit the failed node, change its tool/args, or replace the DAG before resuming."
                ),
                dag=record.pending_review.proposed_dag,
                task_id=task_id,
                pending_review=record.pending_review,
                control_events=[
                    _dag_event(
                        record,
                        "change_review_requested",
                        reason="Execution error review requires a DAG edit before retry.",
                    )
                ],
            )

        for node_id in changed_node_ids:
            if node_id in existing_node_ids:
                _invalidate_patch_results(record, node_id)
        record.dag = prepared
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
        if record.runtime_mode == "dag":
            return await self._continue_dag_loop(
                record,
                result,
                on_token=on_token,
                on_event=on_event,
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
        planning_context: str = "",
        runtime_mode: RuntimeMode = "auto",
    ) -> TaskRecord:
        dag = await self._create_validated_dag(
            _dag_planning_prompt(request, planning_context),
            task_id=task_id,
        )
        record = TaskRecord(
            task_id=dag.task_id,
            user_request=request,
            dag=dag,
            review_level=review_level,
            runtime_mode=runtime_mode,
        )
        self.tasks[record.task_id] = record
        return record

    async def _create_validated_dag(
        self,
        request: str,
        *,
        task_id: str | None = None,
        max_attempts: int = 2,
    ) -> DAG:
        feedback = ""
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            prompt = request if not feedback else _dag_creation_feedback_prompt(request, feedback)
            try:
                dag = await self.dag_agent.aplan(prompt, task_id=task_id)
                return self.prepare_dag_for_review(dag)
            except Exception as exc:
                last_error = exc
                feedback = str(exc)
        assert last_error is not None
        raise last_error

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
        if record.dag.status == "review_required":
            raise DAGExecutionError("DAG contains medium/high risk nodes and is not approved.")
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

                    retry_key = failed_node_id or "__dag__"
                    repair_counts[retry_key] = repair_counts.get(retry_key, 0) + 1
                    if repair_counts[retry_key] > self.max_node_retries:
                        pending = self._create_execution_error_review(
                            record,
                            message=f"Maximum repair retries exceeded for node '{retry_key}'.",
                            error=str(exc),
                            failed_node_id=failed_node_id,
                        )
                        traces.append(_review_trace(record.dag.dag_id, pending))
                        result = RunResult(
                            dag_id=record.dag.dag_id,
                            completed=False,
                            node_results=dict(record.node_results),
                            traces=traces,
                        )
                        break

                    if replan_count >= self.max_replans:
                        pending = self._create_execution_error_review(
                            record,
                            message="Maximum DAG replans exceeded.",
                            error=str(exc),
                            failed_node_id=failed_node_id,
                        )
                        traces.append(_review_trace(record.dag.dag_id, pending))
                        result = RunResult(
                            dag_id=record.dag.dag_id,
                            completed=False,
                            node_results=dict(record.node_results),
                            traces=traces,
                        )
                        break

                    try:
                        proposed_dag = await self._create_next_dag_from_observation(
                            record,
                            last_error=str(exc),
                            failed_node_id=failed_node_id,
                        )
                        applied = self._apply_next_dag_revision(
                            record,
                            proposed_dag,
                            reason="Repair failed node from execution observation.",
                        )
                    except Exception as replan_exc:
                        pending = self._create_execution_error_review(
                            record,
                            message="Node execution failed and DAG agent did not return a usable revised DAG.",
                            error=f"{exc}\nDAG agent error: {replan_exc}",
                            failed_node_id=failed_node_id,
                        )
                        traces.append(_review_trace(record.dag.dag_id, pending))
                        result = RunResult(
                            dag_id=record.dag.dag_id,
                            completed=False,
                            node_results=dict(record.node_results),
                            traces=traces,
                        )
                        break

                    pending = applied.pending_review
                    if pending is not None:
                        traces.append(_review_trace(record.dag.dag_id, pending))
                        result = RunResult(
                            dag_id=record.dag.dag_id,
                            completed=False,
                            node_results=dict(record.node_results),
                            traces=traces,
                        )
                        break
                    traces.append(
                        _dag_revision_trace_event(
                            dag_id=record.dag.dag_id,
                            reason=applied.reason,
                            changed_node_ids=applied.changed_node_ids,
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
                if replan_count >= self.max_replans:
                    continue
                try:
                    proposed_dag = await self._create_next_dag_from_observation(
                        record,
                        last_error="",
                        failed_node_id=None,
                    )
                    applied = self._apply_next_dag_revision(
                        record,
                        proposed_dag,
                        reason="Revise DAG from completed layer observation.",
                    )
                except Exception:
                    continue
                pending = applied.pending_review
                if pending is not None:
                    traces.append(_review_trace(record.dag.dag_id, pending))
                    result = RunResult(
                        dag_id=record.dag.dag_id,
                        completed=False,
                        node_results=dict(record.node_results),
                        traces=traces,
                    )
                    break
                traces.append(
                    _dag_revision_trace_event(
                        dag_id=record.dag.dag_id,
                        reason=applied.reason,
                        changed_node_ids=applied.changed_node_ids,
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
            record.dag.status = _pending_review_dag_status(record.pending_review)
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

    async def _create_next_dag_from_observation(
        self,
        record: TaskRecord,
        *,
        last_error: str,
        failed_node_id: str | None,
    ) -> DAG:
        return await self._create_validated_dag(
            _dag_revision_prompt(
                record,
                last_error=last_error,
                failed_node_id=failed_node_id,
            ),
            task_id=record.task_id,
        )

    def _apply_next_dag_revision(
        self,
        record: TaskRecord,
        next_dag: DAG,
        *,
        reason: str,
    ) -> _DAGRevisionApplyResult:
        proposed_input = next_dag.model_copy(deep=True)
        proposed_input.task_id = record.task_id
        proposed_input.dag_id = record.dag.dag_id
        proposed_input.version = record.dag.version + 1
        proposed = self.prepare_dag_for_review(proposed_input)
        changed_node_ids = _changed_node_ids(record.dag, proposed)
        if not changed_node_ids:
            raise ValueError("DAG agent returned an unchanged DAG revision.")

        if record.suppress_next_review:
            record.suppress_next_review = False
        else:
            policy = review_policy(record.review_level)
            if policy.requires_dag_revision_review(current=record.dag, proposed=proposed):
                pending = PendingReview(
                    review_id=f"review_{uuid4().hex}",
                    kind="dag_replan",
                    message="Review proposed DAG revision from execution observation.",
                    proposed_dag=proposed,
                    payload={"reason": reason, "changed_node_ids": sorted(changed_node_ids)},
                )
                record.pending_review = pending
                record.dag = proposed
                return _DAGRevisionApplyResult(
                    reason=reason,
                    changed_node_ids=changed_node_ids,
                    pending_review=pending,
                )

        for node_id in changed_node_ids:
            if any(node.id == node_id for node in record.dag.nodes):
                _invalidate_patch_results(record, node_id)
        record.dag = proposed
        record.pending_review = None
        return _DAGRevisionApplyResult(
            reason=reason,
            changed_node_ids=changed_node_ids,
        )

    def _create_execution_error_review(
        self,
        record: TaskRecord,
        *,
        message: str,
        error: str,
        failed_node_id: str | None,
    ) -> PendingReview:
        pending = PendingReview(
            review_id=f"review_{uuid4().hex}",
            kind="execution_error",
            message=message,
            proposed_dag=record.dag.model_copy(deep=True),
            payload={
                "error": error,
                "failed_node_id": failed_node_id,
            },
        )
        record.pending_review = pending
        return pending

    async def _handle_control_tool(self, tool_call: ToolCall) -> ControlToolResult:
        if tool_call.name != DAG_AGENT_NAME:
            raise ValueError(f"Unsupported control tool '{tool_call.name}'.")

        request = str(tool_call.arguments.get("request") or "").strip()
        reason = str(tool_call.arguments.get("reason") or "").strip()
        if not request:
            request = "Create a reviewable DAG for the current user task."

        if self._active_continuation_task_id is not None:
            record = self.tasks[self._active_continuation_task_id]
            dag = await self._create_validated_dag(
                _dag_planning_prompt(request, self._conversation_context()),
                task_id=record.task_id,
            )
            record.dag = dag
            record.dag.status = "review_required"
            record.pending_review = None
            record.suppress_next_review = False
            record.continuation_count += 1
            for node in record.dag.nodes:
                record.node_results.pop(node.id, None)
            event = _dag_event(record, "dag_created", reason=reason or "DAG continuation requested.")
            return ControlToolResult(
                content=_dag_created_tool_output(record, reason=reason),
                stop_reason="awaiting_approval",
                events=[event],
            )

        record = await self.create_dag(
            request,
            review_level=self._active_review_level,
            planning_context=self._conversation_context(),
            runtime_mode="auto",
        )
        if self._active_user_message:
            record.user_request = self._active_user_message
        record.dag.status = "review_required"
        event = _dag_event(record, "dag_created", reason=reason)
        return ControlToolResult(
            content=_dag_created_tool_output(record, reason=reason),
            stop_reason="awaiting_approval",
            events=[event],
        )

    async def _continue_dag_loop(
        self,
        record: TaskRecord,
        result: RunResult,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult:
        messages = self.prompt_builder.build(
            PromptRequest(
                profile=self.conversation_profile,
                task_content="{{ user_message }}",
                tools=[],
                memory=self.conversation_profile.memory,
                context=self._conversation_context(),
                variables={
                    "user_message": (
                        "A DAG segment has finished executing in forced DAG mode.\n"
                        f"Original user request:\n{record.user_request}\n\n"
                        f"DAG execution observation:\n{_dag_run_tool_output(record, result)}\n\n"
                        "Decide the next step. If the observations are sufficient, answer the user. "
                        "If more execution is required, call dag_agent to create the next DAG segment. "
                        "Do not call ordinary tools directly in DAG mode."
                    )
                },
            )
        )
        self._active_review_level = record.review_level
        self._active_user_message = record.user_request
        self._active_continuation_task_id = record.task_id
        try:
            loop_result = await self.agent_loop.run(
                "",
                boundary=Boundary(mode="read_only", allowed_paths=["."]),
                allowed_tools=[],
                max_steps=self.max_top_steps,
                messages=messages,
                extra_tools=[dag_agent_tool_definition()],
                control_tool_names={DAG_AGENT_NAME},
                control_tool_handler=self._handle_control_tool,
                on_token=on_token,
                on_event=on_event,
            )
        finally:
            self._active_continuation_task_id = None
            self._active_user_message = ""

        dag_event = _latest_dag_event(loop_result.control_events)
        if dag_event is not None and loop_result.stop_reason == "awaiting_approval":
            return HarnessMessageResult(
                status="awaiting_dag_review",
                message_markdown=_dag_created_review_message(record),
                dag=dag_event.get("dag"),
                run_result=result,
                task_id=record.task_id,
                control_events=[*loop_result.control_events, _dag_event(record, "dag_executed", reason="DAG segment executed.")],
            )

        answer = loop_result.final_response.strip() or _dag_run_fallback_message(record, result)
        return HarnessMessageResult(
            status="completed" if result.completed else "failed",
            message_markdown=answer,
            dag=record.dag,
            run_result=result,
            task_id=record.task_id,
            control_events=[*loop_result.control_events, _dag_event(record, "dag_executed", reason="DAG segment executed.")],
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
        try:
            summary = await asyncio.wait_for(
                self.agent_loop.run(
                    "",
                    boundary=Boundary(mode="read_only", allowed_paths=["."]),
                    max_steps=self.max_top_steps,
                    messages=messages,
                    **stream_kwargs,
                ),
                timeout=60,
            )
        except Exception:
            return _dag_run_fallback_message(record, result)
        return summary.final_response.strip() or _dag_run_fallback_message(record, result)

    def _initial_status(self, dag: DAG) -> str:
        needs_review = any(node.risk in {"medium", "high"} for node in dag.nodes)
        return "review_required" if needs_review else "approved"

    def _conversation_context(self) -> str:
        if not self.tasks:
            return ""
        payload = {
            "recent_dag_tasks": [
                _task_context_payload(record)
                for record in list(self.tasks.values())[-3:]
            ]
        }
        return (
            "Recent DAG execution context is available. Use it to interpret "
            "follow-up requests and DAG planning requests; do not repeat work "
            "whose result is already available unless the user asks to rerun it.\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )


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


def _pending_review_dag_status(pending: PendingReview) -> str:
    if pending.kind in {"dag_replan", "execution_error", "boundary_change"}:
        return "paused_for_replan"
    return "review_required"


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


def _dag_creation_feedback_prompt(request: str, feedback: str) -> str:
    return (
        f"{request}\n\n"
        "The previous DAG proposal failed validation. Return a corrected "
        "compact PlanSpec JSON only.\n"
        f"Validation error: {feedback}"
    )


def _dag_planning_prompt(request: str, context: str) -> str:
    if not context.strip():
        return request
    return (
        f"{request}\n\n"
        "Use this prior conversation and DAG execution context when planning "
        "the new DAG. Treat the user's request above as the current request; "
        "the context below is supporting information, not a replacement task.\n"
        f"{context}"
    )


def _dag_revision_prompt(
    record: TaskRecord,
    *,
    last_error: str,
    failed_node_id: str | None,
) -> str:
    payload = {
        "task_id": record.task_id,
        "user_request": record.user_request,
        "current_dag": record.dag.model_dump(mode="json"),
        "completed_node_results": {
            node_id: {
                "completed": node_result.completed,
                "stop_reason": node_result.stop_reason,
                "final_response": node_result.final_response,
            }
            for node_id, node_result in _completed_results(record.node_results).items()
        },
        "recent_trace_records": [
            trace_record.model_dump(mode="json")
            for trace_record in record.trace_records[-12:]
        ],
        "failed_node_id": failed_node_id,
        "last_error": last_error,
    }
    return (
        "Revise the executable DAG after an execution observation.\n"
        "Return one compact PlanSpec JSON object for the next DAG version.\n"
        "Do not return action types such as keep, patch_node, replace, or abort.\n"
        "Preserve already completed node ids and dependencies when their results "
        "are still needed, and change the failed or downstream nodes so execution "
        "can make progress. If a completed node result can be reused, keep that "
        "node semantically unchanged.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
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


def _dag_revision_trace_event(
    *,
    dag_id: str,
    reason: str,
    changed_node_ids: set[str],
    applied: bool,
) -> TraceEvent:
    return TraceEvent(
        event_id=f"trace_{uuid4().hex}",
        event_type="dag_replanned" if applied else "dag_replan_failed",
        dag_id=dag_id,
        payload={
            "reason": reason,
            "changed_node_ids": sorted(changed_node_ids),
        },
    )


def _task_context_payload(record: TaskRecord) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "dag_id": record.dag.dag_id,
        "user_request": record.user_request,
        "dag_status": record.dag.status,
        "node_results": {
            node_id: {
                "completed": node_result.completed,
                "stop_reason": node_result.stop_reason,
                "final_response": node_result.final_response,
            }
            for node_id, node_result in record.node_results.items()
        },
        "recent_trace_records": [
            {
                "node_id": trace.node_id,
                "tool": trace.tool,
                "status": trace.status,
                "output": trace.output,
                "error": trace.error,
            }
            for trace in record.trace_records[-8:]
        ],
    }


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


def _dag_run_fallback_message(record: TaskRecord, result: RunResult) -> str:
    lines = [
        "DAG execution completed." if result.completed else "DAG execution stopped before completion.",
        "",
        "Node results:",
    ]
    if not result.node_results:
        lines.append("- No completed node results were recorded.")
    for node_id, node_result in result.node_results.items():
        status = "completed" if node_result.completed else "failed"
        response = node_result.final_response.strip() or node_result.stop_reason
        lines.append(f"- `{node_id}` ({status}): {response}")
    return "\n".join(lines)


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


def affected_node_ids_for_patch(dag: DAG, node_id: str) -> set[str]:
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
        if _node_semantic_dump(current_node) != _node_semantic_dump(proposed_node):
            changed.add(node_id)
    if current.edges != proposed.edges:
        changed.update(proposed_nodes)
    return changed


def _node_semantic_dump(node: DAGNode) -> dict[str, Any]:
    dumped = node.model_dump(mode="json")
    dumped.pop("status", None)
    return dumped
