"""DAG agent loop."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from dagent.harness_runtime.dag_executor import (
    DAGExecutionError,
    DAGExecutor,
)
from dagent.harness_runtime.dag_builder import (
    DAGCreationError,
    DAGValidationError,
    dag_from_model_output,
    validate_dag,
)
from dagent.harness_runtime.review_policy import ReviewLevel, review_policy
from dagent.harness_runtime.task_record import ReviewContinuation, RuntimeTaskRecord
from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider, ChatResponse
from dagent.schemas import (
    DAG,
    DAGNode,
    DAGRunResult,
    LoopOutcome,
    LoopStatus,
    PendingReview,
    ToolExecutionRecord,
    ToolInvocation,
    TraceEvent,
)
from dagent.state import PromptBuilder, PromptRequest
from dagent.tools.registry import Tool


MAX_EXECUTION_CONTEXT_CHARS = 16000
MAX_NODE_RESULT_CONTEXT_CHARS = 4000


class DAGAgent:
    """Runtime-facing DAG agent facade."""

    def __init__(
        self,
        *,
        loop: "DAGAgentLoop",
        profile: AgentProfile,
        tools: list[Tool] | None = None,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self.loop = loop
        self.profile = profile
        self.tools = tools or []
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.system_message = self.prompt_builder.build_system_message(
            PromptRequest(
                profile=self.profile,
                task_content="",
                tools=self.tools,
                memory=self.profile.memory,
            )
        )

    @property
    def tasks(self) -> dict[str, RuntimeTaskRecord]:
        return self.loop.tasks

    @tasks.setter
    def tasks(self, value: dict[str, RuntimeTaskRecord]) -> None:
        self.loop.tasks = value

    async def run(
        self,
        request: str,
        *,
        task_id: str | None = None,
        review_level: ReviewLevel = "fast",
        context: str = "",
        runtime_mode: str = "auto",
        conversation_history: list[dict[str, Any]] | None = None,
        force_review: bool = False,
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        initial_messages = self.prompt_builder.build_initial_messages(
            system_message=self.system_message,
            conversation_history=conversation_history,
            current_user_message=self.build_request_user_message(
                prompt=request,
                task_id=resolved_task_id,
            ),
            runtime_context=context,
        )
        return await self.loop.run(
            request,
            task_id=resolved_task_id,
            review_level=review_level,
            dag_messages=initial_messages[1:-1],
            initial_user_message=initial_messages[-1],
            system_message=self.system_message,
            build_user_message=self.build_request_user_message,
            tools=self.tools,
            runtime_mode=runtime_mode,
            force_review=force_review,
            on_token=on_token,
            on_trace=on_trace,
            on_dag=on_dag,
        )

    async def resume_review(
        self,
        state: ReviewContinuation,
        dag: DAG,
        *,
        review_level: ReviewLevel | None = None,
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome | None:
        return await self.loop.resume_review(
            state,
            dag,
            review_level=review_level,
            system_message=self.system_message,
            build_user_message=self.build_request_user_message,
            tools=self.tools,
            on_token=on_token,
            on_trace=on_trace,
            on_dag=on_dag,
        )

    async def execute(
        self,
        task_id: str,
        *,
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
        max_cycles: int | None = None,
    ) -> DAGRunResult:
        return await self.loop.execute(
            task_id,
            system_message=self.system_message,
            build_user_message=self.build_request_user_message,
            tools=self.tools,
            on_token=on_token,
            on_trace=on_trace,
            on_dag=on_dag,
            max_cycles=max_cycles,
        )

    def build_request_user_message(self, *, prompt: str, task_id: str) -> dict[str, str]:
        return self.prompt_builder.build_user_message(
            "Task id: {{ task_id }}\n{{ user_request }}",
            {"user_request": prompt, "task_id": task_id},
        )


class DAGAgentLoop:
    """Owns DAG planning, reviews, execution, and replanning."""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        dag_executor: DAGExecutor,
        max_cycles: int = 6,
    ) -> None:
        self.provider = provider
        self.dag_executor = dag_executor
        self.max_cycles = max_cycles
        self.tasks: dict[str, RuntimeTaskRecord] = {}
        self.runs: dict[str, DAGRunResult] = {}

    async def _request_dag(
        self,
        *,
        task_id: str | None,
        dag_messages: list[dict[str, Any]] | None,
        user_message: dict[str, str],
        system_message: dict[str, str],
        tools: list[Tool],
        allow_no_change: bool = True,
        on_token: Callable[[str], None] | None = None,
    ) -> DAG | str | None:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        if dag_messages is not None:
            messages = [system_message, *dag_messages, user_message]
        else:
            messages = [system_message, user_message]
        response = await _chat_for_dag(self.provider, messages, on_token=on_token)
        if dag_messages is not None:
            dag_messages.append(dict(user_message))
            dag_messages.append({"role": "assistant", "content": response.content})
        result = dag_from_model_output(
            response.content,
            task_id=resolved_task_id,
            tools=tools,
        )
        if result is None:
            if not allow_no_change:
                raise DAGCreationError("DAG agent returned NO_CHANGE for initial planning.")
            return None
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        request: str,
        *,
        task_id: str | None = None,
        review_level: ReviewLevel = "fast",
        dag_messages: list[dict[str, Any]],
        initial_user_message: dict[str, str],
        system_message: dict[str, str],
        build_user_message: Callable[..., dict[str, str]],
        tools: list[Tool],
        runtime_mode: str = "auto",
        force_review: bool = False,
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        response: DAG | str | None = None
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        planning_prompt = request
        cycles_used = 0
        for _ in range(self.max_cycles):
            cycles_used += 1
            try:
                response = await self._request_dag(
                    task_id=task_id,
                    dag_messages=dag_messages,
                    user_message=(
                        initial_user_message
                        if cycles_used == 1
                        else build_user_message(prompt=planning_prompt, task_id=resolved_task_id)
                    ),
                    system_message=system_message,
                    tools=tools,
                    allow_no_change=False,
                    on_token=on_token,
                )
                if isinstance(response, DAG):
                    response = self.prepare_for_review(response)
                break
            except Exception as exc:
                planning_prompt = _format_dag_observation(
                    kind="validation_error",
                    task_id=resolved_task_id,
                    user_request=request,
                    validation_error=str(exc),
                )
        else:
            return _dag_loop_outcome(
                status="failed",
                final_response="DAG planning failed before an executable DAG could be created.",
            )

        if isinstance(response, str):
            return _dag_loop_outcome(
                status="completed",
                final_response=response,
            )

        record = RuntimeTaskRecord.dag_task(
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
            return _dag_loop_outcome(
                status="awaiting_review",
                final_response=_dag_created_review_message(record),
                dag=record.dag,
                task_id=record.task_id,
                pending_review=record.pending_review,
            )

        record.dag.status = "approved"
        _emit_dag(on_dag, record.dag)
        result = await self.execute(
            record.task_id,
            system_message=system_message,
            build_user_message=build_user_message,
            tools=tools,
            on_token=on_token,
            on_trace=on_trace,
            on_dag=on_dag,
            max_cycles=max(0, self.max_cycles - cycles_used),
        )
        return self._finalize_run(record, result)

    async def resume_review(
        self,
        state: ReviewContinuation,
        dag: DAG,
        *,
        review_level: ReviewLevel | None = None,
        system_message: dict[str, str],
        build_user_message: Callable[..., dict[str, str]],
        tools: list[Tool],
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome | None:
        if state.kind not in {"initial_dag", "dag_replan"}:
            return None
        task_id = state.task_id
        if task_id not in self.tasks:
            raise KeyError(f"Unknown task '{task_id}'.")

        record = self.tasks[task_id]
        if (
            record.dag.status == "completed"
            and record.runs
            and record.pending_review is None
        ):
            result = record.runs[-1]
            return _dag_loop_outcome(
                status="completed" if result.completed else "failed",
                final_response=record.final_response or dag_run_fallback_message(record, result),
                dag=record.dag,
                dag_run=result,
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

        result = await self.execute(
            task_id,
            system_message=system_message,
            build_user_message=build_user_message,
            tools=tools,
            on_token=on_token,
            on_trace=on_trace,
            on_dag=on_dag,
        )
        return self._finalize_run(record, result)

    # ------------------------------------------------------------------
    # Unified execution loop
    # ------------------------------------------------------------------

    async def execute(
        self,
        task_id: str,
        *,
        system_message: dict[str, str],
        build_user_message: Callable[..., dict[str, str]],
        tools: list[Tool],
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
        max_cycles: int | None = None,
    ) -> DAGRunResult:
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
                        record.execution_records,
                        valid_node_ids=_dag_node_ids(record.dag),
                    )
                    layer_error = str(exc)

                if not layer_failed:
                    traces.extend(layer.traces)
                    new_node_ids = set(layer.node_results) - set(record.node_results)
                    record.node_results.update(layer.node_results)
                    record.execution_records = self.dag_executor.execution_store.records_for_task(record.task_id)
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

            # Ask LLM for the next step: continue, replan, or finish.
            try:
                response = await self._request_dag(
                    task_id=record.task_id,
                    dag_messages=record.dag_messages,
                    user_message=build_user_message(
                        prompt=pending_observation,
                        task_id=record.task_id,
                    ),
                    system_message=system_message,
                    tools=tools,
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
                record.final_response = response
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

    def _needs_review(self, record: RuntimeTaskRecord, *, force: bool = False) -> bool:
        if force:
            return True
        return review_policy(record.review_level).reviews_dag_changes()

    def _finalize_run(self, record: RuntimeTaskRecord, result: DAGRunResult) -> LoopOutcome:
        if record.pending_review is not None:
            return _dag_loop_outcome(
                status="awaiting_review",
                final_response=record.pending_review.message,
                dag=record.pending_review.proposed_dag,
                dag_run=result,
                task_id=record.task_id,
                pending_review=record.pending_review,
            )
        message = record.final_response or dag_run_fallback_message(record, result)
        return _dag_loop_outcome(
            status="completed" if result.completed else "failed",
            final_response=message,
            dag=record.dag,
            dag_run=result,
            task_id=record.task_id,
        )

    def _absorb_partial_results(
        self,
        record: RuntimeTaskRecord,
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
        record.execution_records = self.dag_executor.execution_store.records_for_task(record.task_id)
        for failed_id in _failed_node_ids(record.execution_records, valid_node_ids=current_node_ids):
            _node_by_id(record.dag, failed_id).status = "failed"
        record.dag.status = "failed"
        _emit_dag(on_dag, record.dag)

    def _apply_replan(self, record: RuntimeTaskRecord, next_dag: DAG) -> None:
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
            if (
                old_node is None
                or old_node.invocation.tool_name != new_node.invocation.tool_name
                or old_node.invocation.arguments != new_node.invocation.arguments
            ):
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
        record: RuntimeTaskRecord,
        traces: list[TraceEvent],
        on_dag: Callable[[DAG], None] | None,
    ) -> DAGRunResult:
        record.execution_records = self.dag_executor.execution_store.records_for_task(record.task_id)
        all_nodes_completed = all(
            nid in record.node_results and record.node_results[nid].completed
            for nid in _dag_node_ids(record.dag)
        )
        completed = record.dag.status != "failed" and (
            bool(record.final_response) or all_nodes_completed
        )
        result = DAGRunResult(
            dag_id=record.dag.dag_id,
            completed=completed,
            node_results=dict(record.node_results),
            traces=traces,
            execution_records=list(record.execution_records),
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
            node.invocation.tool_name
            for node in dag.nodes
            if node.invocation.tool_name and node.invocation.tool_name not in available_tools
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
    """Return visible content from the first <think> block onwards.

    Text before the first <think> block is conversational preamble that is
    not useful to stream. Suppressing it avoids a burst when the think block
    finally arrives.

    Everything from the first <think> onwards is included: think blocks +
    the DAG DSL / answer text between and after them.
    """
    lower = content.lower()
    open_index = lower.find("<think>")
    if open_index == -1:
        return ""
    return content[open_index:]


# ------------------------------------------------------------------
# Observation formatting
# ------------------------------------------------------------------

def _format_dag_observation(
    *,
    kind: str,
    task_id: str | None,
    user_request: str,
    record: RuntimeTaskRecord | None = None,
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
        f"- {execution.node_id} ({execution.invocation.tool_name}): {execution.status}"
        + (f" error={execution.error}" if execution.error else "")
        for execution in record.execution_records[-6:]
    ]
    if recent:
        sections.append("Recent tool executions:\n" + "\n".join(recent))

    return "\n\n".join(sections)


# ------------------------------------------------------------------
# Message formatting
# ------------------------------------------------------------------

def _dag_created_review_message(record: RuntimeTaskRecord) -> str:
    return "\n".join([
        "### DAG ready for review",
        f"- **Task:** `{record.task_id}`",
        f"- **Status:** `{record.dag.status}`",
        f"- **Nodes:** {len(record.dag.nodes)}",
        "- **Next action:** Review and edit the DAG, then confirm to resume execution.",
    ])


def format_dag_execution_context(dag: DAG | None, dag_run: DAGRunResult | None) -> str:
    """Format DAG execution details for validation and fallback output."""
    lines: list[str] = []
    if dag:
        lines.append(f"DAG ({len(dag.nodes)} nodes, status={dag.status}):")
        for node in dag.nodes:
            lines.append(
                f"  - {node.id}: "
                f"{node.invocation.tool_name}({node.invocation.arguments})"
            )
    if dag_run and dag_run.node_results:
        lines.append("Node execution results:")
        for node_id, node_result in dag_run.node_results.items():
            status = "completed" if node_result.completed else "failed"
            response = (
                _context_excerpt(
                    node_result.final_response.strip(),
                    limit=MAX_NODE_RESULT_CONTEXT_CHARS,
                )
                or node_result.stop_reason
            )
            lines.append(f"  - {node_id} ({status}): {response}")
    if not lines:
        return ""
    return _context_excerpt(
        "\n".join(lines),
        limit=MAX_EXECUTION_CONTEXT_CHARS,
    )


def dag_run_fallback_message(record: RuntimeTaskRecord, result: DAGRunResult) -> str:
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

def _dag_loop_outcome(
    *,
    status: LoopStatus,
    final_response: str,
    dag: DAG | None = None,
    dag_run: DAGRunResult | None = None,
    task_id: str | None = None,
    pending_review: PendingReview | None = None,
) -> LoopOutcome:
    events: list[dict[str, Any]] = []
    invocations: list[ToolInvocation] = []
    if dag is not None:
        invocations = [node.invocation for node in dag.nodes]
    if task_id and dag:
        if (
            status == "awaiting_review"
            and pending_review
            and pending_review.kind == "initial_dag"
        ):
            events.append({"kind": "dag_created", "task_id": task_id, "dag": dag})
        elif (
            status == "awaiting_review"
            and pending_review
            and pending_review.kind == "dag_replan"
        ):
            events.append({"kind": "change_review_requested", "task_id": task_id, "dag": dag})
        else:
            events.append({"kind": "dag_executed", "task_id": task_id, "dag": dag})
    return LoopOutcome(
        status=status,
        execution_context=format_dag_execution_context(dag, dag_run),
        final_answer=final_response,
        events=events,
        invocations=invocations,
        dag=dag,
        dag_run=dag_run,
        task_id=task_id,
        pending_review=pending_review,
    )


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


def _dag_completed(record: RuntimeTaskRecord) -> bool:
    return all(
        node.id in record.node_results and record.node_results[node.id].completed
        for node in record.dag.nodes
    )


def _latest_failed_node_id(
    records: list[ToolExecutionRecord],
    *,
    valid_node_ids: set[str] | None = None,
) -> str | None:
    for record in reversed(records):
        if record.status == "failed" and (valid_node_ids is None or record.node_id in valid_node_ids):
            return record.node_id
    return None


def _failed_node_ids(
    records: list[ToolExecutionRecord],
    *,
    valid_node_ids: set[str] | None = None,
) -> set[str]:
    return {
        record.node_id
        for record in records
        if record.status == "failed" and (valid_node_ids is None or record.node_id in valid_node_ids)
    }


def _invalidate_patch_results(record: RuntimeTaskRecord, node_id: str) -> None:
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


def _context_excerpt(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[TRUNCATED after {limit} chars]"
