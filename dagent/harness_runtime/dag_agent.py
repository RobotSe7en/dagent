"""DAG agent loop and PlanSpec compilation helpers."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from dagent.harness_runtime.dag_executor import (
    DAGExecutionError,
    DAGExecutor,
    RunResult,
)
from dagent.harness_runtime.dag_validation import DAGValidationError, validate_dag
from dagent.harness_runtime.review_policy import ReviewLevel, effective_risk, review_policy
from dagent.harness_runtime.task_record import PendingReview, TaskRecord
from dagent.profiles import AgentProfile, ProfileStore
from dagent.providers import ChatProvider, ChatResponse
from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode, NodeExecutionRecord, PlanSpec, TraceEvent
from dagent.state import PromptBuilder, PromptRequest
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
        max_cycles: int = 6,
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
        self.max_cycles = max_cycles
        self.tasks: dict[str, TaskRecord] = {}
        self.runs: dict[str, RunResult] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        request: str,
        *,
        task_id: str | None = None,
        review_level: ReviewLevel = "fast",
        planning_context: str = "",
        runtime_mode: str = "auto",
        dag_messages: list[dict[str, Any]] | None = None,
        force_review: bool = False,
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> DAGAgentLoopResult:
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

        response: DAG | str | None = None
        planning_prompt = request
        cycles_used = 0
        for _ in range(self.max_cycles):
            cycles_used += 1
            try:
                response = await self._request_dag(
                    planning_prompt,
                    task_id=task_id,
                    dag_messages=dag_messages,
                    allow_no_change=False,
                    on_token=on_token,
                )
                break
            except Exception as exc:
                planning_prompt = _format_dag_observation(
                    kind="validation_error",
                    task_id=task_id,
                    user_request=request,
                    planning_context=planning_context,
                    validation_error=str(exc),
                )
        else:
            return DAGAgentLoopResult(
                status="failed",
                message_markdown="DAG planning failed before an executable DAG could be created.",
            )

        if isinstance(response, str):
            return DAGAgentLoopResult(
                status="completed",
                message_markdown=response,
            )

        record = TaskRecord(
            task_id=response.task_id,
            user_request=request,
            dag=response,
            review_level=review_level,
            runtime_mode=runtime_mode,
            dag_messages=dag_messages,
        )
        self.tasks[record.task_id] = record

        if self._needs_review(record, force=force_review):
            record.dag.status = "review_required"
            _emit_dag(on_dag, record.dag)
            record.pending_review = PendingReview(
                review_id=f"review_{uuid4().hex}",
                kind="initial_dag",
                message="Review proposed DAG before execution.",
                proposed_dag=record.dag,
                payload={},
            )
            return DAGAgentLoopResult(
                status="awaiting_dag_review",
                message_markdown=_dag_created_review_message(record),
                dag=record.dag,
                task_id=record.task_id,
                pending_review=record.pending_review,
            )

        record.dag.status = "approved"
        _emit_dag(on_dag, record.dag)
        result = await self.execute(
            record.task_id,
            on_token=on_token,
            on_trace=on_trace,
            on_dag=on_dag,
            max_cycles=max(0, self.max_cycles - cycles_used),
        )
        return self._finalize_run(record, result)

    async def resume(
        self,
        task_id: str,
        dag: DAG,
        review_level: ReviewLevel | None = None,
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> DAGAgentLoopResult:
        if task_id not in self.tasks:
            raise KeyError(f"Unknown task '{task_id}'.")

        record = self.tasks[task_id]
        if (
            record.dag.status == "completed"
            and record.runs
            and record.pending_review is None
        ):
            result = record.runs[-1]
            return DAGAgentLoopResult(
                status="completed" if result.completed else "failed",
                message_markdown=record.message_markdown or dag_run_fallback_message(record, result),
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
        for node_id in changed_node_ids:
            if node_id in existing_node_ids:
                _invalidate_patch_results(record, node_id)
        record.dag = prepared
        record.dag.status = "approved"
        record.pending_review = None
        _emit_dag(on_dag, record.dag)

        result = await self.execute(task_id, on_token=on_token, on_trace=on_trace, on_dag=on_dag)
        return self._finalize_run(record, result)

    # ------------------------------------------------------------------
    # Unified execution loop
    # ------------------------------------------------------------------

    async def execute(
        self,
        task_id: str,
        *,
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
        max_cycles: int | None = None,
    ) -> RunResult:
        record = self.tasks[task_id]
        if record.dag.status == "review_required":
            raise DAGExecutionError("DAG is awaiting review and is not approved.")

        traces: list[TraceEvent] = []
        cycle = 0
        failed_node_id: str | None = None
        cycle_limit = self.max_cycles if max_cycles is None else max_cycles
        pending_observation: str | None = None

        while cycle < cycle_limit:
            cycle += 1

            if pending_observation is None:
                # Execute next ready layer
                layer_failed = False
                try:
                    layer = await self.dag_executor.execute_next_ready_layer(
                        record.dag,
                        initial_results=_completed_results(record.node_results),
                        record_dag_start=not traces,
                        on_trace=on_trace,
                    )
                except Exception as exc:
                    layer_failed = True
                    self._absorb_partial_results(record, traces, on_dag)
                    failed_node_id = _latest_failed_node_id(
                        record.trace_records,
                        valid_node_ids=_dag_node_ids(record.dag),
                    )
                    layer_error = str(exc)

                if not layer_failed:
                    traces.extend(layer.traces)
                    new_node_ids = set(layer.node_results) - set(record.node_results)
                    record.node_results.update(layer.node_results)
                    record.trace_records = self.dag_executor.trace_store.records_for_task(record.task_id)
                    if new_node_ids and all(nid == "start" for nid in new_node_ids):
                        continue
                    layer_error = ""

                pending_observation = _format_dag_observation(
                    kind="layer_failed" if layer_failed else ("dag_executed" if layer.completed else "layer_completed"),
                    task_id=record.task_id,
                    user_request=record.user_request,
                    record=record,
                    last_error=layer_error if layer_failed else "",
                    failed_node_id=failed_node_id if layer_failed else None,
                )

            # Ask LLM for next step — it decides whether to continue, replan, or finish
            try:
                response = await self._request_dag(
                    pending_observation,
                    task_id=record.task_id,
                    dag_messages=record.dag_messages,
                    on_token=on_token,
                )
            except Exception as exc:
                traces.append(_dag_revision_trace_event(dag_id=record.dag.dag_id))
                pending_observation = _format_dag_observation(
                    kind="validation_error",
                    task_id=record.task_id,
                    user_request=record.user_request,
                    record=record,
                    validation_error=str(exc),
                )
                continue

            if isinstance(response, str):
                record.message_markdown = response
                break

            if response is None:
                if _dag_completed(record):
                    break
                pending_observation = None
                continue

            # Apply the new DAG
            try:
                self._apply_replan(record, response)
            except Exception as exc:
                traces.append(_dag_revision_trace_event(dag_id=record.dag.dag_id))
                pending_observation = _format_dag_observation(
                    kind="validation_error",
                    task_id=record.task_id,
                    user_request=record.user_request,
                    record=record,
                    validation_error=str(exc),
                )
                continue
            traces.append(_dag_revision_trace_event(dag_id=record.dag.dag_id))
            _emit_dag(on_dag, record.dag)
            pending_observation = None

            # Review gate
            if record.dag.status == "review_required":
                break

        return self._finalize(record, traces, on_dag)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _needs_review(self, record: TaskRecord, *, force: bool = False) -> bool:
        if force:
            return True
        return review_policy(record.review_level).reviews_dag_changes()

    def _finalize_run(self, record: TaskRecord, result: RunResult) -> DAGAgentLoopResult:
        if record.pending_review is not None:
            return DAGAgentLoopResult(
                status="awaiting_change_review",
                message_markdown=record.pending_review.message,
                dag=record.pending_review.proposed_dag,
                run_result=result,
                task_id=record.task_id,
                pending_review=record.pending_review,
            )
        message = record.message_markdown or dag_run_fallback_message(record, result)
        return DAGAgentLoopResult(
            status="completed" if result.completed else "failed",
            message_markdown=message,
            dag=record.dag,
            run_result=result,
            task_id=record.task_id,
        )

    def _absorb_partial_results(
        self,
        record: TaskRecord,
        traces: list[TraceEvent],
        on_dag: Callable[[DAG], None] | None = None,
    ) -> None:
        traces.extend(self.dag_executor.trace_recorder.events)
        current_node_ids = _dag_node_ids(record.dag)
        for node_id, node_result in self.dag_executor.partial_node_results.items():
            if node_id in current_node_ids:
                record.node_results[node_id] = node_result
                _node_by_id(record.dag, node_id).status = (
                    "completed" if node_result.completed else "failed"
                )
        record.trace_records = self.dag_executor.trace_store.records_for_task(record.task_id)
        for failed_id in _failed_node_ids(record.trace_records, valid_node_ids=current_node_ids):
            _node_by_id(record.dag, failed_id).status = "failed"
        record.dag.status = "failed"
        _emit_dag(on_dag, record.dag)

    async def _request_dag(
        self,
        prompt: str,
        *,
        task_id: str | None,
        dag_messages: list[dict[str, Any]] | None,
        allow_no_change: bool = True,
        on_token: Callable[[str], None] | None = None,
    ) -> DAG | str | None:
        """Call LLM with prompt, return a DAG, a final answer string, or None (NO_CHANGE)."""
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        messages = self.prompt_builder.build(
            PromptRequest(
                profile=self.profile,
                task_content="Task id: {{ task_id }}\n{{ user_request }}",
                tools=self.tools,
                memory=self.profile.memory,
                variables={"user_request": prompt, "task_id": resolved_task_id},
            )
        )
        system_msg = messages[0]
        user_msg = messages[1]
        if dag_messages is not None:
            messages = [system_msg, *dag_messages, user_msg]
        response = await _chat_for_dag(self.provider, messages, on_token=on_token)
        if dag_messages is not None:
            dag_messages.append({"role": "user", "content": prompt})
            dag_messages.append({"role": "assistant", "content": response.content})
        result = dag_from_model_output(response.content, task_id=resolved_task_id, tools=self.tools)
        if result is None:
            if not allow_no_change:
                raise DAGCreationError("DAG agent returned NO_CHANGE for initial planning.")
            return None
        if isinstance(result, str):
            return result
        return self.prepare_for_review(result)

    def _apply_replan(self, record: TaskRecord, next_dag: DAG) -> None:
        prepared = next_dag.model_copy(deep=True)
        prepared.task_id = record.task_id
        prepared.dag_id = record.dag.dag_id
        prepared.version = record.dag.version + 1
        prepared = self.prepare_for_review(prepared)

        changed = _changed_node_ids(record.dag, prepared)
        needs_review = bool(changed) and review_policy(record.review_level).reviews_dag_changes()
        if needs_review:
            prepared.status = "review_required"
        else:
            prepared.status = "approved"

        # Remove stale results for changed/deleted nodes and their downstream
        new_nodes = {node.id: node for node in prepared.nodes}
        old_nodes = {node.id: node for node in record.dag.nodes}
        for nid in list(record.node_results):
            new_node = new_nodes.get(nid)
            if new_node is None:
                _invalidate_patch_results(record, nid)
                continue
            old_node = old_nodes.get(nid)
            if old_node is None or old_node.tool != new_node.tool or old_node.args != new_node.args:
                _invalidate_patch_results(record, nid)

        record.dag = prepared
        if needs_review:
            record.pending_review = PendingReview(
                review_id=f"review_{uuid4().hex}",
                kind="dag_replan",
                message="Review proposed DAG revision from replanning.",
                proposed_dag=prepared,
                payload={},
            )
        else:
            record.pending_review = None

    def _finalize(
        self,
        record: TaskRecord,
        traces: list[TraceEvent],
        on_dag: Callable[[DAG], None] | None,
    ) -> RunResult:
        record.trace_records = self.dag_executor.trace_store.records_for_task(record.task_id)
        all_nodes_completed = all(
            nid in record.node_results and record.node_results[nid].completed
            for nid in _dag_node_ids(record.dag)
        )
        completed = record.dag.status != "failed" and (
            bool(record.message_markdown) or all_nodes_completed
        )
        result = RunResult(
            dag_id=record.dag.dag_id,
            completed=completed,
            node_results=dict(record.node_results),
            traces=traces,
        )
        record.runs.append(result)

        if record.pending_review is not None:
            record.dag.status = "review_required"
        elif record.dag.status != "aborted":
            record.dag.status = "completed" if completed else "failed"
            for node_id, node_result in result.node_results.items():
                try:
                    _node_by_id(record.dag, node_id).status = (
                        "completed" if node_result.completed else "failed"
                    )
                except KeyError:
                    continue

        _emit_dag(on_dag, record.dag)
        self.runs[f"run_{uuid4().hex}"] = result
        return result

    def prepare_for_review(self, dag: DAG) -> DAG:
        prepared = self.dag_executor.normalize(dag)
        validate_dag(prepared)
        self._validate_dag_tools(prepared)
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

# ------------------------------------------------------------------
# LLM communication
# ------------------------------------------------------------------

async def _chat_for_dag(
    provider: ChatProvider,
    messages: list[dict[str, Any]],
    *,
    on_token: Callable[[str], None] | None = None,
) -> ChatResponse:
    if on_token is None or not hasattr(provider, "stream_chat"):
        return await provider.chat(messages)

    content = ""
    visible = ""
    response: ChatResponse | None = None
    async for event in provider.stream_chat(messages):
        if event.type == "token" and event.content:
            content += event.content
            next_visible = _visible_thinking_markup(content)
            if next_visible.startswith(visible):
                delta = next_visible[len(visible):]
                if delta:
                    on_token(delta)
                visible = next_visible
        elif event.type == "done":
            response = event.response
    return response or ChatResponse(content=content)


def _visible_thinking_markup(content: str) -> str:
    parts: list[str] = []
    cursor = 0
    lower = content.lower()
    found_think = False
    while cursor < len(content):
        open_index = lower.find("<think>", cursor)
        if open_index == -1:
            if found_think:
                parts.append(content[cursor:])
            break
        found_think = True
        parts.append(content[cursor:open_index])
        close_index = lower.find("</think>", open_index + len("<think>"))
        if close_index == -1:
            parts.append(content[open_index:])
            cursor = len(content)
            break
        parts.append(content[open_index:close_index + len("</think>")])
        cursor = close_index + len("</think>")
    return "".join(parts)


def _strip_thinking_blocks(content: str) -> str:
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"<think>.*", "", content, flags=re.IGNORECASE | re.DOTALL)


# ------------------------------------------------------------------
# DSL parsing and DAG compilation
# ------------------------------------------------------------------

def _is_no_change(content: str) -> bool:
    cleaned = _strip_thinking_blocks(content).strip().upper()
    return cleaned in {"NO_CHANGE", "NO CHANGE", "NOCHANGE"}


def dag_from_model_output(
    content: str,
    *,
    task_id: str,
    tools: list[Tool] | None = None,
) -> DAG | str | None:
    """Parse LLM output into a DAG, a final answer string, or None (NO_CHANGE)."""
    if _is_no_change(content):
        return None
    try:
        plan = parse_plan_spec_dsl(content)
    except DAGCreationError:
        cleaned = _strip_thinking_blocks(content).strip()
        return cleaned or None
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
        nodes.append(
            {
                "id": match.group("id"),
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
    return [item.strip() for item in deps_text.split(",") if item.strip()]


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
        raise DAGCreationError(f"PlanSpec node '{node.id}' must declare one concrete tool.")
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
    targets = {edge.target for edge in next_edges}
    existing_start_targets = {edge.target for edge in next_edges if edge.source == start_id}
    root_ids = [
        node.id
        for node in next_nodes
        if node.id != start_id and node.id not in targets and node.id not in existing_start_targets
    ]

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
    elif not root_ids:
        return next_nodes, next_edges

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
    if tool_obj is not None and tool_obj.boundary_fn is not None:
        return tool_obj.boundary_fn(args)
    if tool_obj is None:
        return Boundary(mode="read_only")
    paths = [str(args.get(p) or ".") for p in tool_obj.path_args] or ["."]
    if tool_obj.action == "write":
        return Boundary(mode="write_limited", allowed_paths=paths)
    return Boundary(mode="read_only", allowed_paths=paths)


# ------------------------------------------------------------------
# Observation formatting
# ------------------------------------------------------------------

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


# ------------------------------------------------------------------
# Message formatting
# ------------------------------------------------------------------

def _dag_created_review_message(record: TaskRecord) -> str:
    return "\n".join([
        "### DAG ready for review",
        f"- **Task:** `{record.task_id}`",
        f"- **Status:** `{record.dag.status}`",
        f"- **Nodes:** {len(record.dag.nodes)}",
        "- **Next action:** Review and edit the DAG, then confirm to resume execution.",
    ])


def dag_run_fallback_message(record: TaskRecord, result: RunResult) -> str:
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


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------

def _emit_dag(on_dag: Callable[[DAG], None] | None, dag: DAG) -> None:
    if on_dag is not None:
        on_dag(dag.model_copy(deep=True))


def _dag_revision_trace_event(*, dag_id: str) -> TraceEvent:
    return TraceEvent(
        event_id=f"trace_{uuid4().hex}",
        event_type="dag_replanned",
        dag_id=dag_id,
        payload={},
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


def _dag_node_ids(dag: DAG) -> set[str]:
    return {node.id for node in dag.nodes}


def _dag_completed(record: TaskRecord) -> bool:
    return all(
        node.id in record.node_results and record.node_results[node.id].completed
        for node in record.dag.nodes
    )


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
