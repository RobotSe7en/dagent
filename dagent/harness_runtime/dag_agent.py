"""DAG agent loop and PlanSpec compilation helpers."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from dagent.harness_runtime.dag_executor import (
    DAGExecutionError,
    DAGExecutor,
    RunResult,
    _inject_placeholders,
    _next_ready_nodes,
)
from dagent.harness_runtime.dag_validation import DAGValidationError, validate_dag
from dagent.harness_runtime.review_policy import ReviewLevel, review_policy
from dagent.harness_runtime.task_record import PendingReview, TaskRecord
from dagent.profiles import AgentProfile, ProfileStore
from dagent.providers import ChatProvider
from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode, NodeExecutionRecord, PermissionRequest, PlanSpec, TraceEvent
from dagent.state import PromptBuilder, PromptRequest
from dagent.harness_runtime.review_policy import effective_risk
from dagent.tools.registry import Tool


class DAGCreationError(ValueError):
    """Raised when a proposed DAG cannot become an executable tool DAG."""


_PLAN_NODE_RE = re.compile(
    r"^(?P<id>[A-Za-z][A-Za-z0-9_-]*)\s*=\s*"
    r"(?P<tool>[A-Za-z_][A-Za-z0-9_]*)\((?P<args>.*)\)"
    r"(?:\s+after\s+(?P<deps>.+))?$"
)


@dataclass(frozen=True)
class DAGAgentLoopResult:
    status: str
    message_markdown: str
    dag: DAG | None = None
    run_result: RunResult | None = None
    task_id: str | None = None
    pending_review: PendingReview | None = None


class DAGAgentLoop:
    """Owns the DAG agent conversation, reviews, execution, and replanning."""

    def __init__(
        self,
        provider: ChatProvider,
        *,
        dag_executor: DAGExecutor,
        profile: AgentProfile | None = None,
        profile_store: ProfileStore | None = None,
        profile_name: str = "dag_agent",
        prompt_builder: PromptBuilder | None = None,
        tools: list[Tool] | None = None,
        max_replans: int = 3,
        max_node_retries: int = 2,
    ) -> None:
        self.provider = provider
        self.dag_executor = dag_executor
        self.profile = profile or (profile_store or ProfileStore()).load(profile_name)
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.tools = tools or (
            dag_executor.tool_executor.registry.all_tools()
            if dag_executor.tool_executor is not None
            else []
        )
        self.max_replans = max_replans
        self.max_node_retries = max_node_retries
        self.tasks: dict[str, TaskRecord] = {}
        self.runs: dict[str, RunResult] = {}

    async def run(
        self,
        request: str,
        *,
        task_id: str | None = None,
        review_level: ReviewLevel = "balanced",
        planning_context: str = "",
        runtime_mode: str = "auto",
        dag_messages: list[dict[str, Any]] | None = None,
        force_review: bool = False,
    ) -> DAGAgentLoopResult:
        record = await self._create_record(
            request,
            task_id=task_id,
            review_level=review_level,
            planning_context=planning_context,
            runtime_mode=runtime_mode,
            dag_messages=dag_messages,
        )
        if force_review or review_policy(review_level).requires_initial_dag_review(record.dag):
            record.dag.status = "review_required"
            return DAGAgentLoopResult(
                status="awaiting_dag_review",
                message_markdown=_dag_created_review_message(record),
                dag=record.dag,
                task_id=record.task_id,
            )

        record.dag.status = "approved"
        result = await self.execute(record.task_id)
        if record.pending_review is not None:
            return DAGAgentLoopResult(
                status="awaiting_change_review",
                message_markdown=record.pending_review.message,
                dag=record.pending_review.proposed_dag,
                run_result=result,
                task_id=record.task_id,
                pending_review=record.pending_review,
            )
        return DAGAgentLoopResult(
            status="completed" if result.completed else "failed",
            message_markdown=_dag_run_fallback_message(record, result),
            dag=record.dag,
            run_result=result,
            task_id=record.task_id,
        )

    async def resume(
        self,
        task_id: str,
        dag: DAG,
        review_level: ReviewLevel | None = None,
    ) -> DAGAgentLoopResult:
        if task_id not in self.tasks:
            raise KeyError(f"Unknown task '{task_id}'.")

        record = self.tasks[task_id]
        if (
            record.dag.status == "completed"
            and record.runs
            and record.pending_review is None
            and record.pending_permission_request is None
        ):
            result = record.runs[-1]
            return DAGAgentLoopResult(
                status="completed" if result.completed else "failed",
                message_markdown=record.message_markdown or _dag_run_fallback_message(record, result),
                dag=record.dag,
                run_result=result,
                task_id=task_id,
            )
        if review_level is not None:
            record.review_level = review_level
        submitted = dag.model_copy(deep=True)
        submitted.task_id = task_id
        submitted.dag_id = record.dag.dag_id
        submitted.version = record.dag.version + 1
        prepared = self.prepare_for_review(submitted)

        existing_node_ids = {node.id for node in record.dag.nodes}
        changed_node_ids = _changed_node_ids(record.dag, prepared)
        if (
            record.pending_review is not None
            and record.pending_review.kind == "execution_error"
            and not changed_node_ids
        ):
            record.dag.status = _pending_review_dag_status(record.pending_review)
            return DAGAgentLoopResult(
                status="awaiting_change_review",
                message_markdown=(
                    "The failed DAG was confirmed without any node or edge changes. "
                    "Edit the failed node, change its tool/args, or replace the DAG before resuming."
                ),
                dag=record.pending_review.proposed_dag,
                task_id=task_id,
                pending_review=record.pending_review,
            )

        for node_id in changed_node_ids:
            if node_id in existing_node_ids:
                _invalidate_patch_results(record, node_id)
        record.dag = prepared
        record.dag.status = "approved"
        record.pending_review = None
        record.suppress_next_review = True

        result = await self.execute(task_id)
        if record.pending_review is not None:
            return DAGAgentLoopResult(
                status="awaiting_change_review",
                message_markdown=record.pending_review.message,
                dag=record.pending_review.proposed_dag,
                run_result=result,
                task_id=task_id,
                pending_review=record.pending_review,
            )
        if not result.completed:
            return DAGAgentLoopResult(
                status="failed",
                message_markdown=_dag_run_fallback_message(record, result),
                dag=record.dag,
                run_result=result,
                task_id=task_id,
            )
        return DAGAgentLoopResult(
            status="completed",
            message_markdown=_dag_run_fallback_message(record, result),
            dag=record.dag,
            run_result=result,
            task_id=task_id,
        )

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

    async def _create_record(
        self,
        request: str,
        *,
        task_id: str | None = None,
        review_level: ReviewLevel = "balanced",
        planning_context: str = "",
        runtime_mode: str = "auto",
        dag_messages: list[dict[str, Any]] | None = None,
    ) -> TaskRecord:
        if dag_messages is None:
            dag_messages = []
        if planning_context.strip():
            dag_messages.append({
                "role": "user",
                "content": _format_dag_observation(
                    kind="planning_context",
                    task_id=task_id,
                    user_request=request,
                    planning_context=planning_context,
                ),
            })
        dag = await self._create_validated_dag(
            request,
            task_id=task_id,
            dag_messages=dag_messages,
        )
        record = TaskRecord(
            task_id=dag.task_id,
            user_request=request,
            dag=dag,
            review_level=review_level,
            runtime_mode=runtime_mode,
            dag_messages=dag_messages,
        )
        self.tasks[record.task_id] = record
        return record

    async def _create_validated_dag(
        self,
        request: str,
        *,
        task_id: str | None = None,
        dag_messages: list[dict[str, Any]] | None = None,
        max_attempts: int = 2,
    ) -> DAG:
        feedback = ""
        last_error: Exception | None = None
        for _ in range(max_attempts):
            prompt = request if not feedback else _format_dag_observation(
                kind="validation_error",
                task_id=task_id,
                user_request=request,
                validation_error=feedback,
            )
            try:
                dag = await self._ask_model_for_dag(
                    prompt,
                    task_id=task_id,
                    dag_messages=dag_messages,
                )
                if dag is None:
                    raise DAGCreationError("DAG agent returned NO_CHANGE for initial planning.")
                return self.prepare_for_review(dag)
            except Exception as exc:
                last_error = exc
                feedback = str(exc)
        assert last_error is not None
        raise last_error

    async def _ask_model_for_dag(
        self,
        user_request: str,
        *,
        task_id: str | None,
        dag_messages: list[dict[str, Any]] | None,
    ) -> DAG | None:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        messages = self.prompt_builder.build(
            PromptRequest(
                profile=self.profile,
                task_content=(
                    "Task id: {{ task_id }}\n"
                    "{{ user_request }}"
                ),
                tools=self.tools,
                memory=self.profile.memory,
                variables={
                    "user_request": user_request,
                    "task_id": resolved_task_id,
                },
            )
        )
        system_msg = messages[0]
        user_msg = messages[1]
        if dag_messages is not None:
            messages = [system_msg, *dag_messages, user_msg]
        response = await self.provider.chat(messages)
        if dag_messages is not None:
            dag_messages.append({"role": "user", "content": user_request})
            dag_messages.append({"role": "assistant", "content": response.content})
        return dag_from_model_output(response.content, task_id=resolved_task_id, tools=self.tools)

    def prepare_for_review(self, dag: DAG) -> DAG:
        prepared = self.dag_executor.normalize(dag)
        validate_dag(prepared)
        self._validate_dag_tools(prepared)
        prepared.status = self._initial_status(prepared)
        return prepared

    def _validate_dag_tools(self, dag: DAG) -> None:
        if self.dag_executor.tool_executor is None:
            return
        available_tools = self.dag_executor.tool_executor.registry.names()
        unknown_tools = sorted({
            node.tool
            for node in dag.nodes
            if node.tool and node.tool not in available_tools
        })
        if unknown_tools:
            raise DAGValidationError(
                "Unknown tool(s): "
                f"{', '.join(unknown_tools)}. "
                "Available tools: "
                f"{', '.join(sorted(available_tools))}."
            )

    def approve_dag(self, task_id: str) -> DAG:
        record = self.tasks[task_id]
        record.dag.status = "approved"
        return record.dag

    async def execute(self, task_id: str) -> RunResult:
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
                    current_node_ids = _dag_node_ids(record.dag)
                    record.node_results.update(
                        {
                            node_id: node_result
                            for node_id, node_result in self.dag_executor.partial_node_results.items()
                            if node_id in current_node_ids
                        }
                    )
                    for node_id, node_result in self.dag_executor.partial_node_results.items():
                        if node_id in current_node_ids:
                            _node_by_id(record.dag, node_id).status = "completed" if node_result.completed else "failed"
                    record.trace_records = self.dag_executor.trace_store.records_for_task(record.task_id)
                    failed_node_id = _latest_failed_node_id(record.trace_records, valid_node_ids=current_node_ids)
                    for failed_id in _failed_node_ids(record.trace_records, valid_node_ids=current_node_ids):
                        _node_by_id(record.dag, failed_id).status = "failed"

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
                        proposed_dag = await self._replan_after_layer(
                            record,
                            last_error=str(exc),
                            failed_node_id=failed_node_id,
                        )
                        if proposed_dag is None:
                            raise ValueError("Replan returned NO_CHANGE on a failed layer.")
                        reason = "Repair failed node from execution observation."
                        self._apply_replan(record, proposed_dag)
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

                    traces.append(_dag_revision_trace_event(dag_id=record.dag.dag_id, reason=reason))
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
                prev_count = len(record.node_results)
                record.node_results.update(result.node_results)
                record.trace_records = self.dag_executor.trace_store.records_for_task(record.task_id)
                if result.pending_permission_request is not None or result.completed:
                    break
                if len(record.node_results) == prev_count:
                    break

                if replan_count < self.max_replans:
                    try:
                        proposed_dag = await self._replan_after_layer(record)
                    except Exception:
                        proposed_dag = None
                    if proposed_dag is not None:
                        reason = "Replan after successful layer execution."
                        self._apply_replan(record, proposed_dag)
                        traces.append(_dag_revision_trace_event(dag_id=record.dag.dag_id, reason=reason))
                        replan_count += 1
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
        elif policy.requires_node_execution_review() and any(node.tool != "dag_start" for node in ready_nodes):
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

    async def _replan_after_layer(
        self,
        record: TaskRecord,
        *,
        last_error: str = "",
        failed_node_id: str | None = None,
        max_attempts: int = 2,
    ) -> DAG | None:
        observation = _format_dag_observation(
            kind="layer_failed" if last_error or failed_node_id else "layer_completed",
            task_id=record.task_id,
            user_request=record.user_request,
            record=record,
            last_error=last_error,
            failed_node_id=failed_node_id,
        )
        feedback = ""
        last_exc: Exception | None = None
        for _ in range(max_attempts):
            prompt = observation if not feedback else _format_dag_observation(
                kind="validation_error",
                task_id=record.task_id,
                user_request=record.user_request,
                record=record,
                validation_error=feedback,
            )
            dag = await self._ask_model_for_dag(
                prompt,
                task_id=record.task_id,
                dag_messages=record.dag_messages,
            )
            if dag is None:
                return None
            try:
                return self.prepare_for_review(dag)
            except Exception as exc:
                last_exc = exc
                feedback = str(exc)
        assert last_exc is not None
        raise last_exc

    def _apply_replan(self, record: TaskRecord, next_dag: DAG) -> None:
        prepared = next_dag.model_copy(deep=True)
        prepared.task_id = record.task_id
        prepared.dag_id = record.dag.dag_id
        prepared.version = record.dag.version + 1
        prepared = self.prepare_for_review(prepared)
        policy = review_policy(record.review_level)
        if policy.requires_initial_dag_review(prepared):
            prepared.status = "review_required"
        new_nodes = {node.id: node for node in prepared.nodes}
        old_nodes = {node.id: node for node in record.dag.nodes}
        for nid in list(record.node_results):
            new_node = new_nodes.get(nid)
            if new_node is None:
                del record.node_results[nid]
                continue
            old_node = old_nodes.get(nid)
            if old_node is None or old_node.tool != new_node.tool or old_node.args != new_node.args:
                del record.node_results[nid]
        record.dag = prepared
        if prepared.status == "review_required":
            record.pending_review = PendingReview(
                review_id=f"review_{uuid4().hex}",
                kind="dag_replan",
                message="Review proposed DAG revision from replanning.",
                proposed_dag=prepared,
                payload={},
            )
        else:
            record.pending_review = None

    def _create_execution_error_review(
        self,
        record: TaskRecord,
        *,
        message: str,
        error: str,
        failed_node_id: str | None,
    ) -> PendingReview:
        proposed_dag = record.dag.model_copy(deep=True)
        proposed_dag.status = "paused_for_replan"
        pending = PendingReview(
            review_id=f"review_{uuid4().hex}",
            kind="execution_error",
            message=message,
            proposed_dag=proposed_dag,
            payload={"error": error, "failed_node_id": failed_node_id},
        )
        record.pending_review = pending
        return pending

    def _initial_status(self, dag: DAG) -> str:
        needs_review = any(node.risk in {"medium", "high"} for node in dag.nodes)
        return "review_required" if needs_review else "approved"


def _is_no_change(content: str) -> bool:
    cleaned = _strip_thinking_blocks(content).strip().upper()
    return cleaned in {"NO_CHANGE", "NO CHANGE", "NOCHANGE"}



def dag_from_model_output(
    content: str,
    *,
    task_id: str,
    tools: list[Tool] | None = None,
) -> DAG | None:
    if _is_no_change(content):
        return None
    plan = parse_plan_spec_dsl(content)
    return compile_plan_spec(plan, task_id=task_id, tools=tools)


def parse_plan_spec_dsl(content: str) -> PlanSpec:
    lines = _plan_spec_lines(content)
    task = ""
    nodes = []
    for line_number, line in lines:
        if line.lower().startswith("task:"):
            task = line.split(":", 1)[1].strip()
            continue
        match = _PLAN_NODE_RE.match(line)
        if not match:
            raise DAGCreationError(f"PlanSpec DSL line {line_number} is invalid: {line}")
        node_id = match.group("id")
        nodes.append(
            {
                "id": node_id,
                "tool": match.group("tool"),
                "args": _parse_args(match.group("args"), line_number=line_number),
                "depends_on": _parse_depends_on(match.group("deps")),
            }
        )

    if not nodes:
        raise DAGCreationError("PlanSpec DSL must contain at least one node line.")
    return PlanSpec.model_validate({"task": task, "nodes": nodes})


def _plan_spec_lines(content: str) -> list[tuple[int, str]]:
    stripped = _strip_thinking_blocks(content.strip())
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:planspec|text)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    if "PLAN_SPEC" in stripped:
        _, stripped = stripped.split("PLAN_SPEC", 1)
    if "END_PLAN_SPEC" in stripped:
        stripped, _ = stripped.split("END_PLAN_SPEC", 1)

    lines = []
    for line_number, raw_line in enumerate(stripped.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.lower().startswith("task:") and _PLAN_NODE_RE.match(line) is None:
            continue
        lines.append((line_number, line))
    return lines


def _strip_thinking_blocks(content: str) -> str:
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"<think>.*", "", content, flags=re.IGNORECASE | re.DOTALL)


def _parse_args(args_text: str, *, line_number: int) -> dict:
    args_text = args_text.strip()
    if not args_text:
        return {}
    try:
        expr = ast.parse(f"_tool({args_text})", mode="eval").body
    except SyntaxError as exc:
        raise DAGCreationError(f"PlanSpec DSL line {line_number} has invalid args syntax.") from exc
    if not isinstance(expr, ast.Call):
        raise DAGCreationError(f"PlanSpec DSL line {line_number} has invalid args syntax.")
    if len(expr.args) == 1 and isinstance(expr.args[0], ast.Dict) and not expr.keywords:
        try:
            value = ast.literal_eval(expr.args[0])
        except (ValueError, TypeError) as exc:
            raise DAGCreationError(f"PlanSpec DSL line {line_number} has invalid args value.") from exc
        if not isinstance(value, dict):
            raise DAGCreationError(f"PlanSpec DSL line {line_number} args must be an object.")
        return value
    if expr.args:
        raise DAGCreationError(f"PlanSpec DSL line {line_number} only supports keyword args.")
    parsed = {}
    for keyword in expr.keywords:
        if keyword.arg is None:
            raise DAGCreationError(f"PlanSpec DSL line {line_number} does not support **kwargs.")
        try:
            parsed[keyword.arg] = ast.literal_eval(keyword.value)
        except (ValueError, TypeError) as exc:
            raise DAGCreationError(f"PlanSpec DSL line {line_number} has invalid args value.") from exc
    return parsed


def _parse_depends_on(deps_text: str | None) -> list[str]:
    if not deps_text:
        return []
    return [
        item.strip()
        for item in deps_text.split(",")
        if item.strip()
    ]


def compile_plan_spec(
    plan: PlanSpec,
    *,
    task_id: str,
    tools: list[Tool] | None = None,
) -> DAG:
    tool_index = {t.name: t for t in (tools or [])}
    nodes = [_compile_plan_node(node, tool_index=tool_index) for node in plan.nodes]
    edges = [
        DAGEdge(
            source=dependency,
            target=node.id,
            reason=f"{node.id} depends on {dependency}.",
        )
        for node in plan.nodes
        for dependency in node.depends_on
    ]
    if len(nodes) > 1:
        nodes, edges = _ensure_start_node(nodes, edges)
    return DAG(
        dag_id=f"dag_{uuid4().hex}",
        task_id=task_id,
        status="draft",
        nodes=nodes,
        edges=edges,
    )


def _compile_plan_node(
    node,
    *,
    tool_index: dict[str, Tool] | None = None,
) -> DAGNode:
    if not node.tool:
        raise DAGCreationError(
            f"PlanSpec node '{node.id}' must declare one concrete tool."
        )
    args = dict(node.args)
    registered = (tool_index or {}).get(node.tool)
    boundary = _infer_boundary(registered, args)

    risk = effective_risk(registered, args)

    return DAGNode(
        id=node.id,
        tool=node.tool,
        args=args,
        boundary=boundary,
        risk=risk,
    )


def _ensure_start_node(
    nodes: list[DAGNode],
    edges: list[DAGEdge],
) -> tuple[list[DAGNode], list[DAGEdge]]:
    node_ids = {node.id for node in nodes}
    start_id = "start"
    next_nodes = list(nodes)
    next_edges = list(edges)
    if start_id not in node_ids:
        next_nodes = [
            DAGNode(
                id=start_id,
                tool="dag_start",
                args={},
                boundary=Boundary(mode="read_only"),
                risk="low",
            ),
            *next_nodes,
        ]
        node_ids.add(start_id)

    targets = {edge.target for edge in next_edges}
    existing_start_targets = {edge.target for edge in next_edges if edge.source == start_id}
    root_ids = [
        node.id
        for node in next_nodes
        if node.id != start_id and node.id not in targets and node.id not in existing_start_targets
    ]
    next_edges.extend(
        DAGEdge(
            source=start_id,
            target=node_id,
            reason=f"{node_id} starts after start.",
        )
        for node_id in root_ids
    )
    return next_nodes, next_edges


def _infer_boundary(tool_obj: Tool | None, args: dict) -> Boundary:
    """Infer boundary from tool registration info.

    If the tool provides a custom boundary_fn, use it.
    Otherwise, derive boundary from Tool.action and Tool.path_args.
    """
    if tool_obj is not None and tool_obj.boundary_fn is not None:
        return tool_obj.boundary_fn(args)
    if tool_obj is None:
        return Boundary(mode="read_only")
    paths = [str(args.get(p) or ".") for p in tool_obj.path_args] or ["."]
    if tool_obj.action == "write":
        return Boundary(mode="write_limited", allowed_paths=paths)
    return Boundary(mode="read_only", allowed_paths=paths)

def _pending_review_dag_status(pending: PendingReview) -> str:
    if pending.kind in {"dag_replan", "execution_error", "boundary_change"}:
        return "paused_for_replan"
    return "review_required"


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


def _format_dag_observation(
    *,
    kind: str,
    task_id: str | None,
    user_request: str,
    record: TaskRecord | None = None,
    planning_context: str = "",
    last_error: str = "",
    failed_node_id: str | None = None,
    validation_error: str = "",
) -> str:
    sections = [
        f"DAG observation: {kind}",
        f"Task id: {task_id or (record.task_id if record else '<new>')}",
    ]
    if user_request:
        sections.append(f"User request:\n{user_request}")
    if planning_context:
        sections.append(f"Planning context:\n{planning_context.strip()}")
    if validation_error:
        sections.append(f"Validation error:\n{validation_error}")
    if record is None:
        return "\n\n".join(sections)

    completed = [
        f"- {node_id}: {result.final_response.strip()[:500]}"
        for node_id, result in _completed_results(record.node_results).items()
    ]
    if completed:
        sections.append("Completed node outputs:\n" + "\n".join(completed))
    if failed_node_id:
        sections.append(f"Failed node: {failed_node_id}")
    if last_error:
        sections.append(f"Error:\n{last_error}")

    recent = [
        f"- {trace.node_id} ({trace.tool}): {trace.status}"
        + (f" error={trace.error}" if trace.error else "")
        for trace in record.trace_records[-6:]
    ]
    if recent:
        sections.append("Recent trace:\n" + "\n".join(recent))

    return "\n\n".join(sections)


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


def _dag_revision_trace_event(*, dag_id: str, reason: str) -> TraceEvent:
    return TraceEvent(
        event_id=f"trace_{uuid4().hex}",
        event_type="dag_replanned",
        dag_id=dag_id,
        payload={"reason": reason},
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


def _dag_node_ids(dag: DAG) -> set[str]:
    return {node.id for node in dag.nodes}


def _latest_failed_node_id(
    records: list[NodeExecutionRecord],
    *,
    valid_node_ids: set[str] | None = None,
) -> str | None:
    for record in reversed(records):
        if record.status == "failed" and (valid_node_ids is None or record.node_id in valid_node_ids):
            return record.node_id
    return None


def _failed_node_ids(
    records: list[NodeExecutionRecord],
    *,
    valid_node_ids: set[str] | None = None,
) -> set[str]:
    return {
        record.node_id
        for record in records
        if record.status == "failed" and (valid_node_ids is None or record.node_id in valid_node_ids)
    }


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


