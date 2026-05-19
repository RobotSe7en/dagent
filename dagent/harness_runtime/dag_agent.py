"""DAG agent and loop."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from dagent.capabilities.toolsets import CapabilityToolAdapter
from dagent.harness_runtime.artifacts import create_run_workspace, init_artifact_states
from dagent.harness_runtime.dag_executor import (
    DAGExecutionError,
    DAGExecutor,
)
from dagent.harness_runtime.dag_builder import (
    DAGCreationError,
    DAGValidationError,
    compile_dag_spec,
    dag_from_model_output,
    validate_dag,
)
from dagent.harness_runtime.review_policy import ReviewLevel, review_policy
from dagent.harness_runtime.task_record import ReviewContinuation, RuntimeTaskRecord
from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider, ChatResponse
from dagent.schemas import (
    Artifact,
    ArtifactState,
    DAG,
    DAGNode,
    DAGSpec,
    LoopOutcome,
    LoopStatus,
    PendingReview,
    CapabilityDefinition,
    CapabilityInvocation,
    RunTrace,
    RunTraceNode,
    RunTraceStatus,
)
from dagent.schemas.node import NodeStatus
from dagent.state import PromptBuilder, PromptRequest


MAX_EXECUTION_CONTEXT_CHARS = 16000
MAX_NODE_RESULT_CONTEXT_CHARS = 4000


class DAGAgent:
    """Runtime-facing DAG agent facade."""

    def __init__(
        self,
        *,
        loop: "DAGAgentLoop",
        profile: AgentProfile,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self.loop = loop
        self.profile = profile
        self.tools = self.loop.available_capabilities()
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.system_message = self.prompt_builder.build_system_message(
            PromptRequest(
                profile=self.profile,
                task_content="",
                tools=self.tools,
                memory=self.profile.memory,
            )
        )
        self.messages: list[dict[str, Any]] = [dict(self.system_message)]

    async def run(
        self,
        request: str,
        *,
        task_id: str | None = None,
        review_level: ReviewLevel = "fast",
        runtime_mode: str = "auto",
        force_review: bool = False,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        return await self.loop.run_dynamic(
            request,
            task_id=resolved_task_id,
            review_level=review_level,
            messages=self.messages,
            build_user_message=self.build_request_user_message,
            runtime_mode=runtime_mode,
            force_review=force_review,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )

    async def resume_review(
        self,
        state: ReviewContinuation,
        *,
        record: RuntimeTaskRecord,
        dag: DAG | None = None,
        approved: bool = True,
        review_level: ReviewLevel | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome | None:
        return await self.loop.resume_review(
            state,
            record,
            dag,
            approved=approved,
            review_level=review_level,
            messages=self.messages,
            build_user_message=self.build_request_user_message,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )

    async def execute(
        self,
        record: RuntimeTaskRecord,
        *,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
        max_cycles: int | None = None,
    ) -> RunTrace | None:
        return await self.loop.execute(
            record,
            messages=self.messages,
            build_user_message=self.build_request_user_message,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
            max_cycles=max_cycles,
        )

    def build_request_user_message(self, *, prompt: str, task_id: str) -> dict[str, str]:
        return self.prompt_builder.build_user_message(
            "Task id: {{ task_id }}\n{{ user_request }}",
            {"user_request": prompt, "task_id": task_id},
        )


class DAGAgentLoop:
    """Owns dynamic and static DAG lifecycle orchestration."""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        dag_executor: DAGExecutor,
        tool_adapter: CapabilityToolAdapter,
        enabled_toolsets: Sequence[str] = ("builtin",),
        max_cycles: int = 6,
    ) -> None:
        self.provider = provider
        self.dag_executor = dag_executor
        self.tool_adapter = tool_adapter
        self.enabled_toolsets = tuple(enabled_toolsets)
        self.max_cycles = max_cycles

    def available_capabilities(self) -> list[CapabilityDefinition]:
        return self.tool_adapter.capabilities(self.enabled_toolsets)

    async def _request_dag(
        self,
        *,
        task_id: str | None,
        messages: list[dict[str, Any]],
        user_message: dict[str, str],
        allow_no_change: bool = True,
        on_token: Callable[[str], None] | None = None,
    ) -> DAG | str | None:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        messages.append(dict(user_message))
        response = await _chat_for_dag(self.provider, messages, on_token=on_token)
        messages.append({"role": "assistant", "content": response.content})
        result = dag_from_model_output(
            response.content,
            task_id=resolved_task_id,
            tools=self.tool_adapter.capabilities(self.enabled_toolsets),
        )
        if result is None:
            if not allow_no_change:
                raise DAGCreationError("DAG agent returned NO_CHANGE for initial planning.")
            return None
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_dynamic(
        self,
        request: str,
        *,
        task_id: str | None = None,
        review_level: ReviewLevel = "fast",
        messages: list[dict[str, Any]],
        build_user_message: Callable[..., dict[str, str]],
        runtime_mode: str = "auto",
        force_review: bool = False,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        record = RuntimeTaskRecord.dag_task(
            task_id=resolved_task_id,
            user_request=request,
            dag=_seed_dag(resolved_task_id),
            review_level=review_level,
            runtime_mode=runtime_mode,
        )
        result = await self.execute(
            record,
            messages=messages,
            build_user_message=build_user_message,
            entry_observation=request,
            force_review=force_review,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )
        return self._finalize_run(record, result)

    async def run_static(
        self,
        spec: DAGSpec,
        *,
        workspace_root: str | Path = ".dagent-runs",
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        run_id = f"dag_run_{uuid4().hex}"
        dag = compile_dag_spec(
            spec,
            task_id=run_id,
            capabilities=self.tool_adapter.capabilities(self.enabled_toolsets),
        )
        dag = self.prepare_for_review(dag)
        workspace = create_run_workspace(workspace_root)
        artifact_states = init_artifact_states(spec.artifacts)
        dag.status = "approved"
        record = RuntimeTaskRecord.dag_task(
            task_id=run_id,
            user_request=f"Run DAGSpec {spec.id}",
            dag=dag,
            review_level="fast",
            runtime_mode="dag_spec",
            spec_id=spec.id,
            workspace_path=str(workspace),
        )
        _emit_dag(on_dag, dag)

        dag_executor = DAGExecutor(
            capability_executor=self.dag_executor.capability_executor,
            workspace_path=workspace,
            artifacts=spec.artifacts,
            artifact_states=artifact_states,
            spec_id=spec.id,
        )
        trace = await self.execute(
            record,
            replan=False,
            dag_executor=dag_executor,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )
        if trace.status == "completed" and _has_required_artifact_failure(
            spec.artifacts,
            trace.artifacts,
        ):
            trace.root.status = "failed"
            record.dag.status = "failed"
        if trace.status == "failed":
            _mark_planned_artifacts_failed(
                dag_executor.artifact_states,
                "DAG execution failed.",
            )
            trace.artifacts = dict(dag_executor.artifact_states)
        _emit_dag(on_dag, record.dag)
        return _dag_loop_outcome(
            status="completed" if trace.status == "completed" else "failed",
            final_response=dag_run_fallback_message(record, trace),
            dag=record.dag,
            trace=trace,
            task_id=run_id,
            spec_id=spec.id,
            workspace_path=str(workspace),
        )

    async def resume_review(
        self,
        state: ReviewContinuation,
        record: RuntimeTaskRecord,
        dag: DAG | None,
        *,
        approved: bool,
        review_level: ReviewLevel | None = None,
        messages: list[dict[str, Any]],
        build_user_message: Callable[..., dict[str, str]],
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome | None:
        if state.kind not in {"initial_dag", "dag_replan"}:
            return None
        task_id = state.task_id
        if record.task_id != task_id:
            raise ValueError(
                f"Review state for task '{task_id}' cannot resume task '{record.task_id}'."
            )
        if (
            record.dag.status == "completed"
            and record.trace is not None
            and record.pending_review is None
        ):
            trace = record.trace
            return _dag_loop_outcome(
                status="completed" if trace.status == "completed" else "failed",
                final_response=_trace_output(trace) or dag_run_fallback_message(record, trace),
                dag=record.dag,
                trace=trace,
                task_id=task_id,
            )
        if review_level is not None:
            record.review_level = review_level

        if not approved:
            record.pending_review = None
            observation = _format_dag_observation(
                kind="review_denied",
                task_id=task_id,
                record=record,
                review_message="Human reviewer denied the proposed DAG change. Continue without applying it.",
            )
            outcome = await self._continue_from_observation(
                record,
                observation,
                messages=messages,
                build_user_message=build_user_message,
                on_token=on_token,
                on_event=on_event,
                on_dag=on_dag,
                extra_events=[
                    {
                        "kind": "review_denied",
                        "task_id": task_id,
                        "review_kind": state.kind,
                    }
                ],
            )
            return outcome

        if dag is None:
            raise ValueError("Approved DAG review requires a submitted DAG.")

        submitted = dag.model_copy(deep=True)
        submitted.task_id = task_id
        submitted.dag_id = record.dag.dag_id
        submitted.version = record.dag.version + 1
        prepared = self.prepare_for_review(submitted)

        existing_node_ids = {node.id for node in record.dag.nodes}
        changed_node_ids = _changed_node_ids(record.dag, prepared)
        for node_id in changed_node_ids:
            if node_id in existing_node_ids:
                _invalidate_node_results(record, node_id)
        record.dag = prepared
        record.dag.status = "approved"
        record.pending_review = None
        _emit_dag(on_dag, record.dag)

        result = await self.execute(
            record,
            messages=messages,
            build_user_message=build_user_message,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )
        return self._finalize_run(record, result)

    # ------------------------------------------------------------------
    # Unified execution loop
    # ------------------------------------------------------------------

    async def execute(
        self,
        record: RuntimeTaskRecord,
        *,
        messages: list[dict[str, Any]] | None = None,
        build_user_message: Callable[..., dict[str, str]] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
        max_cycles: int | None = None,
        replan: bool = True,
        dag_executor: DAGExecutor | None = None,
        entry_observation: str | None = None,
        force_review: bool = False,
    ) -> RunTrace | None:
        if record.pending_review is not None:
            raise DAGExecutionError("DAG is awaiting review and is not approved.")
        if entry_observation is not None and not replan:
            raise TypeError("entry_observation requires replan=True.")
        self._validate_dag_tools(record.dag)
        if replan and (messages is None or build_user_message is None):
            raise TypeError("Dynamic DAG execution requires messages and build_user_message.")

        cycle = 0
        failed_node_id: str | None = None
        cycle_limit = self.max_cycles if max_cycles is None else max_cycles
        pending_observation: str | None = entry_observation
        trace = record.trace
        active_executor = dag_executor or self.dag_executor

        while cycle < cycle_limit:
            cycle += 1

            if pending_observation is None:
                # Execute next ready layer
                layer_failed = False
                before_count = len(trace.root.children) if trace is not None else 0
                try:
                    previous_node_ids = set(_node_traces_by_id(trace)) if trace is not None else set()
                    layer = await active_executor.execute_next_ready_layer(
                        record.dag,
                        initial_trace=trace,
                        on_token=on_token,
                        on_event=on_event,
                    )
                except Exception as exc:
                    layer_failed = True
                    trace = self._absorb_partial_results(
                        record,
                        trace,
                        on_dag,
                        dag_executor=active_executor,
                    )
                    failed_node_id = _latest_failed_node_id_from_trace(
                        trace,
                        valid_node_ids=_dag_node_ids(record.dag),
                    )
                    layer_error = str(exc)

                if not layer_failed:
                    trace = layer
                    new_node_ids = set(_node_traces_by_id(layer)) - previous_node_ids
                    record.trace = layer
                    if new_node_ids and all(
                        _node_by_id(record.dag, nid).node_type == "start"
                        for nid in new_node_ids
                    ):
                        # Synthetic start nodes are bookkeeping, not an execution/replan turn.
                        cycle -= 1
                        continue
                    layer_error = ""

                pending_observation = _format_dag_observation(
                    kind="layer_failed" if layer_failed else ("dag_executed" if layer.status == "completed" else "layer_completed"),
                    task_id=record.task_id,
                    record=record,
                    last_error=layer_error if layer_failed else "",
                    failed_node_id=failed_node_id if layer_failed else None,
                )

            if not replan:
                if layer_failed:
                    break
                if trace is not None and trace.status == "completed":
                    break
                if trace is not None and len(trace.root.children) == before_count:
                    trace.root.status = "failed"
                    record.dag.status = "failed"
                    break
                pending_observation = None
                continue

            # Ask LLM for the next step: continue, replan, or finish.
            try:
                response = await self._request_dag(
                    task_id=record.task_id,
                    user_message=build_user_message(
                        prompt=pending_observation,
                        task_id=record.task_id,
                    ),
                    messages=messages,
                    allow_no_change=_has_real_nodes(record.dag),
                    on_token=on_token,
                )
            except Exception as exc:
                pending_observation = _format_dag_observation(
                    kind="validation_error",
                    task_id=record.task_id,
                    record=record,
                    validation_error=str(exc),
                )
                continue

            if isinstance(response, str):
                trace = _set_trace_output(record, trace, response)
                break

            if response is None:
                if _dag_completed(record):
                    break
                pending_observation = None
                continue

            # Apply the new DAG
            try:
                self._apply_replan(record, response, force_review=force_review)
            except Exception as exc:
                pending_observation = _format_dag_observation(
                    kind="validation_error",
                    task_id=record.task_id,
                    record=record,
                    validation_error=str(exc),
                )
                continue
            _emit_dag(on_dag, record.dag)
            pending_observation = None

            # Review gate
            if record.pending_review is not None:
                break

        return self._finalize(record, trace, on_dag)

    async def _continue_from_observation(
        self,
        record: RuntimeTaskRecord,
        observation: str,
        *,
        messages: list[dict[str, Any]],
        build_user_message: Callable[..., dict[str, str]],
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
        extra_events: list[dict[str, Any]] | None = None,
    ) -> LoopOutcome:
        result = await self.execute(
            record,
            messages=messages,
            build_user_message=build_user_message,
            entry_observation=observation,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )
        return self._finalize_run(record, result, extra_events=extra_events)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _finalize_run(
        self,
        record: RuntimeTaskRecord,
        result: RunTrace | None,
        *,
        extra_events: list[dict[str, Any]] | None = None,
    ) -> LoopOutcome:
        if record.pending_review is not None:
            return _dag_loop_outcome(
                status="awaiting_review",
                final_response=record.pending_review.message,
                dag=record.pending_review.proposed_dag,
                trace=result,
                task_id=record.task_id,
                pending_review=record.pending_review,
                extra_events=extra_events,
            )
        message = _trace_output(result) or dag_run_fallback_message(record, result)
        outcome_dag = record.dag if _has_real_nodes(record.dag) else None
        return _dag_loop_outcome(
            status="completed" if result.status == "completed" else "failed",
            final_response=message,
            dag=outcome_dag,
            trace=result,
            task_id=record.task_id,
            extra_events=extra_events,
        )

    def _absorb_partial_results(
        self,
        record: RuntimeTaskRecord,
        trace: RunTrace | None,
        on_dag: Callable[[DAG], None] | None = None,
        *,
        dag_executor: DAGExecutor | None = None,
    ) -> RunTrace:
        if trace is None:
            trace = _empty_run_trace(run_id=record.task_id, dag=record.dag)
        current_node_ids = _dag_node_ids(record.dag)
        active_executor = dag_executor or self.dag_executor
        for node_id, node_trace in active_executor.partial_node_traces.items():
            if node_id in current_node_ids:
                _replace_or_append_trace_child(trace.root, node_trace)
                _node_by_id(record.dag, node_id).status = _node_status_from_trace(node_trace.status)
        for failed_id in _failed_node_ids_from_trace(trace, valid_node_ids=current_node_ids):
            _node_by_id(record.dag, failed_id).status = "failed"
        record.dag.status = "failed"
        trace.root.status = "failed"
        record.trace = trace
        _emit_dag(on_dag, record.dag)
        return trace

    def _apply_replan(
        self,
        record: RuntimeTaskRecord,
        next_dag: DAG,
        *,
        force_review: bool = False,
    ) -> None:
        is_initial = not _has_real_nodes(record.dag)
        prepared = next_dag.model_copy(deep=True)
        prepared.task_id = record.task_id
        prepared.dag_id = record.dag.dag_id
        prepared.version = record.dag.version + 1
        prepared = self.prepare_for_review(prepared)

        changed = _changed_node_ids(record.dag, prepared)
        needs_review = bool(changed) and (
            (is_initial and force_review)
            or review_policy(record.review_level).reviews_dag_changes()
        )
        if needs_review:
            prepared.status = "review_required"
        else:
            prepared.status = "approved"

        # Remove stale results for changed/deleted nodes and their downstream.
        # This must use the old DAG before replacement so downstream invalidation
        # follows the graph that produced the cached results.
        old_node_ids = {node.id for node in record.dag.nodes}
        for node_id in changed:
            if node_id in old_node_ids:
                _invalidate_node_results(record, node_id)

        record.dag = prepared
        if needs_review:
            record.pending_review = PendingReview(
                review_id=f"review_{uuid4().hex}",
                kind="initial_dag" if is_initial else "dag_replan",
                message=(
                    "Review proposed DAG before execution."
                    if is_initial
                    else "Review proposed DAG revision from replanning."
                ),
                proposed_dag=prepared,
                payload={},
            )
        else:
            record.pending_review = None

    def _finalize(
        self,
        record: RuntimeTaskRecord,
        trace: RunTrace | None,
        on_dag: Callable[[DAG], None] | None,
    ) -> RunTrace | None:
        if record.pending_review is not None and trace is None and record.trace is None:
            record.dag.status = "review_required"
            _emit_dag(on_dag, record.dag)
            return None
        trace = trace or record.trace or _empty_run_trace(run_id=record.task_id, dag=record.dag)
        node_traces = _node_traces_by_id(trace)
        dag_node_ids = _dag_node_ids(record.dag)
        all_nodes_completed = bool(dag_node_ids) and all(
            nid in node_traces and node_traces[nid].status == "completed"
            for nid in dag_node_ids
        )
        completed = record.dag.status != "failed" and (
            bool(_trace_output(trace)) or all_nodes_completed
        )
        trace.root.status = "completed" if completed else "failed"
        record.trace = trace

        if record.pending_review is not None:
            record.dag.status = "review_required"
        elif record.dag.status != "aborted":
            record.dag.status = "completed" if completed else "failed"
            _sync_dag_node_statuses(record.dag, node_traces)

        _emit_dag(on_dag, record.dag)
        return trace

    def prepare_for_review(self, dag: DAG) -> DAG:
        prepared = self.dag_executor.normalize(dag)
        validate_dag(prepared)
        self._validate_dag_tools(prepared)
        return prepared

    def _validate_dag_tools(self, dag: DAG) -> None:
        unknown_tools = sorted({
            node.invocation.capability_id
            for node in dag.nodes
            if node.invocation.capability_id
            and not self._is_enabled_capability(node.invocation.capability_id)
        })
        if unknown_tools:
            available_capabilities = [
                definition.id
                for definition in self.tool_adapter.capabilities(self.enabled_toolsets)
            ]
            raise DAGValidationError(
                "Unknown capability(s): "
                f"{', '.join(unknown_tools)}. "
                "Available capabilities: "
                f"{', '.join(sorted(available_capabilities))}."
            )

    def _is_enabled_capability(self, capability_id: str) -> bool:
        try:
            self.tool_adapter.ensure_allowed(
                capability_id,
                enabled_toolsets=self.enabled_toolsets,
            )
            return True
        except KeyError:
            return False

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
    response: ChatResponse | None = None
    async for event in provider.stream_chat(messages):
        if event.type == "token" and event.content:
            content += event.content
            on_token(event.content)
        elif event.type == "done":
            response = event.response
    return response or ChatResponse(content=content)


# ------------------------------------------------------------------
# Observation formatting
# ------------------------------------------------------------------

def _format_dag_observation(
    *,
    kind: str,
    task_id: str | None,
    record: RuntimeTaskRecord | None = None,
    last_error: str = "",
    failed_node_id: str | None = None,
    validation_error: str = "",
    review_message: str = "",
) -> str:
    sections = [
        f"DAG observation: {kind}",
        f"Task id: {task_id or (record.task_id if record else '<new>')}",
    ]
    if validation_error:
        sections.append(f"Validation error:\n{validation_error}")
    if review_message:
        sections.append(f"Review message:\n{review_message}")
    if record is None:
        return "\n\n".join(sections)

    completed = [
        f"- {node_id}: {str(node_trace.output or '').strip()[:500]}"
        for node_id, node_trace in _completed_node_traces(record).items()
    ]
    if completed:
        sections.append("Completed node outputs:\n" + "\n".join(completed))
    if failed_node_id:
        sections.append(f"Failed node: {failed_node_id}")
    if last_error:
        sections.append(f"Error:\n{last_error}")

    recent = _recent_capability_summaries(record.trace, limit=6)
    if recent:
        sections.append("Recent capability executions:\n" + "\n".join(recent))

    return "\n\n".join(sections)


def format_dag_execution_context(dag: DAG | None, trace: RunTrace | None) -> str:
    """Format DAG execution details for validation and fallback output."""
    lines: list[str] = []
    if dag:
        lines.append(f"DAG ({len(dag.nodes)} nodes, status={dag.status}):")
        for node in dag.nodes:
            if node.node_type == "start":
                lines.append(f"  - {node.id}: internal start")
                continue
            lines.append(
                f"  - {node.id}: "
                f"{node.invocation.capability_id}({node.invocation.arguments})"
            )
    if trace is not None:
        node_traces = _node_traces_by_id(trace)
    else:
        node_traces = {}
    if node_traces:
        lines.append("Node execution results:")
        for node_id, node_trace in node_traces.items():
            status = node_trace.status
            response = (
                _context_excerpt(
                    str(node_trace.output or "").strip(),
                    limit=MAX_NODE_RESULT_CONTEXT_CHARS,
                )
                or (node_trace.error.message if node_trace.error else status)
            )
            lines.append(f"  - {node_id} ({status}): {response}")
    if not lines:
        return ""
    return _context_excerpt(
        "\n".join(lines),
        limit=MAX_EXECUTION_CONTEXT_CHARS,
    )


def dag_run_fallback_message(record: RuntimeTaskRecord, trace: RunTrace) -> str:
    node_traces = _node_traces_by_id(trace)
    lines = [
        "DAG execution completed." if trace.status == "completed" else "DAG execution stopped before completion.",
        "",
        "Node results:",
    ]
    if not node_traces:
        lines.append("- No completed node results were recorded.")
    for node_id, node_trace in node_traces.items():
        status = node_trace.status
        response = str(node_trace.output or "").strip() or (
            node_trace.error.message if node_trace.error else status
        )
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
    trace: RunTrace | None = None,
    task_id: str | None = None,
    spec_id: str | None = None,
    workspace_path: str | None = None,
    pending_review: PendingReview | None = None,
    extra_events: list[dict[str, Any]] | None = None,
) -> LoopOutcome:
    events: list[dict[str, Any]] = list(extra_events or [])
    invocations: list[CapabilityInvocation] = []
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
        execution_context=format_dag_execution_context(dag, trace),
        final_answer=final_response,
        events=events,
        invocations=invocations,
        dag=dag,
        trace=trace,
        task_id=task_id,
        spec_id=spec_id,
        workspace_path=workspace_path,
        pending_review=pending_review,
    )


def _emit_dag(on_dag: Callable[[DAG], None] | None, dag: DAG) -> None:
    if on_dag is not None:
        on_dag(dag.model_copy(deep=True))


def _has_required_artifact_failure(
    artifacts: dict[str, Artifact],
    states: dict[str, ArtifactState],
) -> bool:
    for artifact_id, artifact in artifacts.items():
        if not artifact.required:
            continue
        state = states.get(artifact_id)
        if state is None or state.status in {"missing", "failed"}:
            return True
    return False


def _mark_planned_artifacts_failed(
    states: dict[str, ArtifactState],
    error: str,
) -> None:
    for artifact_id, state in list(states.items()):
        if state.status == "planned":
            states[artifact_id] = state.model_copy(
                update={"status": "failed", "error": error}
            )


def _completed_node_traces(record: RuntimeTaskRecord) -> dict[str, RunTraceNode]:
    trace = record.trace
    if trace is None:
        return {}
    return {
        node_id: node_trace
        for node_id, node_trace in _node_traces_by_id(trace).items()
        if node_trace.status == "completed"
    }


def _node_by_id(dag: DAG, node_id: str):
    for node in dag.nodes:
        if node.id == node_id:
            return node
    raise KeyError(node_id)


_TRACE_TO_NODE_STATUS: dict[RunTraceStatus, NodeStatus] = {
    "planned": "planned",
    "running": "running",
    "completed": "completed",
    "failed": "failed",
    "skipped": "skipped",
    "awaiting_review": "running",
    "cancelled": "skipped",
}


def _node_status_from_trace(status: RunTraceStatus) -> NodeStatus:
    """Map a run-trace status onto the narrower DAG node status enum."""
    return _TRACE_TO_NODE_STATUS.get(status, "planned")


def _sync_dag_node_statuses(dag: DAG, node_traces: dict[str, RunTraceNode]) -> None:
    """Write each trace node's resolved status back onto its DAG node."""
    for node_id, node_trace in node_traces.items():
        try:
            node = _node_by_id(dag, node_id)
        except KeyError:
            continue
        node.status = _node_status_from_trace(node_trace.status)


def _empty_run_trace(*, run_id: str, dag: DAG) -> RunTrace:
    root = RunTraceNode.run(run_id=run_id, status="running")
    root.ref["dag_id"] = dag.dag_id
    return RunTrace(run_id=run_id, root=root)


def _set_trace_output(
    record: RuntimeTaskRecord,
    trace: RunTrace | None,
    output: str,
) -> RunTrace:
    trace = trace or record.trace or _empty_run_trace(run_id=record.task_id, dag=record.dag)
    trace.root.output = output
    record.trace = trace
    return trace


def _trace_output(trace: RunTrace) -> str:
    return str(trace.root.output or "").strip()


def _node_traces_by_id(trace: RunTrace | None) -> dict[str, RunTraceNode]:
    if trace is None:
        return {}
    return {
        node.ref["node_id"]: node
        for node in trace.root.children
        if node.kind == "dag_node" and node.ref.get("node_id")
    }


def _replace_or_append_trace_child(parent: RunTraceNode, child: RunTraceNode) -> None:
    for index, existing in enumerate(parent.children):
        if existing.kind == child.kind and existing.ref == child.ref:
            parent.children[index] = child
            return
    parent.children.append(child)


def _dag_node_ids(dag: DAG) -> set[str]:
    return {node.id for node in dag.nodes}


def _seed_dag(task_id: str) -> DAG:
    return DAG(
        dag_id=f"dag_{uuid4().hex}",
        task_id=task_id,
        version=0,
        status="approved",
        nodes=[
            DAGNode(
                id="start",
                node_type="start",
                invocation=CapabilityInvocation(capability_id="", kind="tool"),
            )
        ],
    )


def _has_real_nodes(dag: DAG) -> bool:
    return any(node.node_type != "start" for node in dag.nodes)


def _dag_completed(record: RuntimeTaskRecord) -> bool:
    trace = record.trace
    node_traces = _node_traces_by_id(trace) if trace is not None else {}
    return all(
        node.id in node_traces and node_traces[node.id].status == "completed"
        for node in record.dag.nodes
    )


def _latest_failed_node_id_from_trace(
    trace: RunTrace | None,
    *,
    valid_node_ids: set[str] | None = None,
) -> str | None:
    for node_id, node_trace in reversed(list(_node_traces_by_id(trace).items())):
        if node_trace.status == "failed" and (valid_node_ids is None or node_id in valid_node_ids):
            return node_id
    return None


def _failed_node_ids_from_trace(
    trace: RunTrace | None,
    *,
    valid_node_ids: set[str] | None = None,
) -> set[str]:
    return {
        node_id
        for node_id, node_trace in _node_traces_by_id(trace).items()
        if node_trace.status == "failed" and (valid_node_ids is None or node_id in valid_node_ids)
    }


def _recent_capability_summaries(trace: RunTrace | None, *, limit: int) -> list[str]:
    if trace is None:
        return []
    summaries: list[str] = []

    def visit(node: RunTraceNode, current_node_id: str | None = None) -> None:
        next_node_id = node.ref.get("node_id") if node.kind == "dag_node" else current_node_id
        if node.kind == "capability_call" and node.capability_execution is not None:
            invocation = node.capability_execution.invocation
            detail = f"- {next_node_id or '<tool>'} ({invocation.capability_id}): {node.status}"
            if node.error is not None:
                detail += f" error={node.error.message}"
            elif node.capability_execution.result and node.capability_execution.result.error:
                detail += f" error={node.capability_execution.result.error}"
            summaries.append(detail)
        for child in node.children:
            visit(child, next_node_id)

    visit(trace.root)
    return summaries[-limit:]


def _invalidate_node_results(record: RuntimeTaskRecord, node_id: str) -> None:
    trace = record.trace
    for affected_id in _downstream_node_ids(record.dag, node_id):
        if trace is not None:
            trace.root.children = [
                child
                for child in trace.root.children
                if child.kind != "dag_node" or child.ref.get("node_id") != affected_id
            ]
        try:
            _node_by_id(record.dag, affected_id).status = "ready"
        except KeyError:
            continue


def _downstream_node_ids(dag: DAG, node_id: str) -> set[str]:
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
