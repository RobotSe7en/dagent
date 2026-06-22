"""DAG agent and loop."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from dagent.capabilities.toolsets import CapabilityToolAdapter
from dagent.harness_runtime.capability_scope import CapabilityScope, DEFAULT_CAPABILITY_SCOPE
from dagent.harness_runtime.artifacts import (
    ArtifactUpload,
    create_run_workspace,
    init_artifact_states,
    materialize_artifact_uploads,
)
from dagent.harness_runtime.dag_executor import (
    DAGExecutionError,
    DAGExecutor,
    SETTLED_NODE_STATUSES,
)
from dagent.harness_runtime.dag_builder import (
    DAGCreationError,
    DAGValidationError,
    MAX_EXECUTION_CONTEXT_CHARS,
    compile_dag_spec,
    context_excerpt,
    dag_from_model_output,
    validate_dag,
)
from dagent.review import ReviewLevel, _append_reviewer_feedback, _review_policy
from dagent.harness_runtime.capability_scope import capability_scope_from_state, capability_scope_to_state
from dagent.harness_runtime.runtime_events import ResponseStreamContext, response_token_stream
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
    CapabilityNodePayload,
    RunTrace,
    RunTraceError,
    RunTraceNode,
    RunTraceStatus,
    RunState,
    StartNodePayload,
    iter_dag_invocations,
)
from dagent.config import DEFAULT_RUNS_DIR, resolve_run_workspace_root
from dagent.schemas.node import NodeStatus
from dagent.state import PromptBuilder, PromptRequest


MAX_NODE_RESULT_CONTEXT_CHARS = 4000
MAX_CAPABILITY_ARGS_CONTEXT_CHARS = 2000


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
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.tools = self.loop.available_capabilities()
        self.system_message = self._build_system_message(DEFAULT_CAPABILITY_SCOPE)
        self.messages: list[dict[str, Any]] = []

    def _build_system_message(self, capability_scope: CapabilityScope) -> dict[str, str]:
        tools = self.loop.available_capabilities(capability_scope.capability_ids)
        return self.prompt_builder.build_system_message(
            PromptRequest(
                profile=self.profile,
                task_content="",
                tools=tools,
            )
        )

    def _provider_messages(
        self,
        messages: list[dict[str, Any]],
        capability_scope: CapabilityScope,
    ) -> list[dict[str, Any]]:
        system_message = self._build_system_message(capability_scope)
        return [dict(system_message), *[dict(message) for message in messages]]

    async def run(
        self,
        request: str,
        *,
        task_id: str | None = None,
        review_level: ReviewLevel = "fast",
        runtime_mode: str = "auto",
        dynamic_adjust: bool = True,
        workspace_path: str | Path | None = None,
        dag_executor: DAGExecutor | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        return await self.run_messages(
            [{"role": "user", "content": request}],
            task_id=task_id,
            review_level=review_level,
            runtime_mode=runtime_mode,
            dynamic_adjust=dynamic_adjust,
            workspace_path=workspace_path,
            dag_executor=dag_executor,
            capability_scope=capability_scope,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )

    async def run_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        task_id: str | None = None,
        review_level: ReviewLevel = "fast",
        runtime_mode: str = "auto",
        dynamic_adjust: bool = True,
        workspace_path: str | Path | None = None,
        dag_executor: DAGExecutor | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        request = _last_user_content(messages)
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        self.messages = _messages_before_last_user(messages)
        provider_messages = self._provider_messages(self.messages, capability_scope)
        outcome = await self.loop.run_dynamic(
            request,
            task_id=resolved_task_id,
            review_level=review_level,
            capability_scope=capability_scope,
            messages=provider_messages,
            build_user_message=self.build_request_user_message,
            runtime_mode=runtime_mode,
            dynamic_adjust=dynamic_adjust,
            workspace_path=workspace_path,
            dag_executor=dag_executor,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )
        internal_messages = _strip_system_message(provider_messages)
        self.messages = internal_messages
        state = outcome.state.model_copy(update={"internal_messages": internal_messages})
        return outcome.model_copy(update={"state": state})

    async def resume_review(
        self,
        state: RunState,
        *,
        dag: DAG | None = None,
        approved: bool = True,
        review_level: ReviewLevel | None = None,
        feedback: str | None = None,
        dag_executor: DAGExecutor | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome | None:
        self.messages = [dict(message) for message in state.internal_messages]
        provider_messages = self._provider_messages(
            self.messages,
            capability_scope_from_state(state.capability_scope),
        )
        outcome = await self.loop.resume_review(
            state,
            dag,
            approved=approved,
            review_level=review_level,
            feedback=feedback,
            messages=provider_messages,
            build_user_message=self.build_request_user_message,
            dag_executor=dag_executor,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )
        if outcome is None:
            return None
        internal_messages = _strip_system_message(provider_messages)
        self.messages = internal_messages
        next_state = outcome.state.model_copy(update={"internal_messages": internal_messages})
        return outcome.model_copy(update={"state": next_state})

    async def execute(
        self,
        record: RunState,
        *,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
        max_cycles: int | None = None,
    ) -> RunTrace | None:
        return await self.loop.execute(
            record,
            messages=self._provider_messages(
                self.messages,
                capability_scope_from_state(record.capability_scope),
            ),
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

    def available_capabilities(
        self,
        capability_ids: Sequence[str] | None = None,
    ) -> list[CapabilityDefinition]:
        return self.tool_adapter.capabilities(
            self.enabled_toolsets,
            capability_ids=capability_ids,
        )

    async def _request_dag(
        self,
        *,
        task_id: str | None,
        messages: list[dict[str, Any]],
        user_message: dict[str, str],
        allow_no_change: bool = True,
        model_step: int | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
    ) -> DAG | str | None:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        messages.append(dict(user_message))
        response = await _chat_for_dag(
            self.provider,
            messages,
            run_id=resolved_task_id,
            model_step=model_step,
            on_token=on_token,
            on_event=on_event,
        )
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": response.content,
        }
        if response.reasoning_content:
            assistant_message["reasoning_content"] = response.reasoning_content
        messages.append(assistant_message)
        result = dag_from_model_output(
            response.content,
            task_id=resolved_task_id,
            tools=self.tool_adapter.capabilities(
                self.enabled_toolsets,
                capability_ids=capability_scope.capability_ids,
            ),
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
        dynamic_adjust: bool = True,
        workspace_path: str | Path | None = None,
        dag_executor: DAGExecutor | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        record = RunState(
            run_id=resolved_task_id,
            kind="dynamic_dag",
            status="completed",
            user_request=request,
            dag=_seed_dag(resolved_task_id),
            review_level=review_level,
            runtime_mode=runtime_mode,  # type: ignore[arg-type]
            dynamic_adjust=dynamic_adjust,
            workspace_path=None if workspace_path is None else str(workspace_path),
            capability_scope=capability_scope_to_state(capability_scope),
        )
        result = await self.execute(
            record,
            messages=messages,
            build_user_message=build_user_message,
            entry_observation=request,
            dag_executor=dag_executor,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )
        return self._finalize_run(record, result)

    async def run_static(
        self,
        spec: DAGSpec,
        *,
        run_id: str | None = None,
        graph_input: Any = None,
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        run_id = run_id or f"dag_run_{uuid4().hex}"
        dag = compile_dag_spec(
            spec,
            task_id=run_id,
            capabilities=self.tool_adapter.capabilities(self.enabled_toolsets),
        )
        dag = self.prepare_for_review(dag)
        capability_workspace_root = (
            self.dag_executor.capability_workspace_root
            or self.dag_executor.capability_executor.workspace_root
        )
        workspace = create_run_workspace(
            resolve_run_workspace_root(capability_workspace_root, workspace_root),
            run_id=run_id,
        )
        artifact_states = init_artifact_states(spec.artifacts)
        materialized_artifact_ids = materialize_artifact_uploads(
            artifact_uploads or {},
            artifacts=spec.artifacts,
            workspace_path=workspace,
        )
        for artifact_id in materialized_artifact_ids:
            artifact = spec.artifacts[artifact_id]
            artifact_states[artifact_id] = ArtifactState(
                id=artifact.id,
                paths=list(artifact.paths),
                status="created",
            )
        dag.status = "approved"
        record = RunState(
            run_id=run_id,
            kind="static_dag",
            status="completed",
            user_request=f"Run DAGSpec {spec.id}",
            dag=dag,
            review_level="fast",
            runtime_mode="dag_spec",
            spec_id=spec.id,
            workspace_path=str(workspace),
            capability_scope=capability_scope_to_state(DEFAULT_CAPABILITY_SCOPE),
        )
        _emit_dag(on_dag, dag)

        dag_executor = DAGExecutor(
            capability_executor=self.dag_executor.capability_executor,
            workspace_path=workspace,
            capability_workspace_root=capability_workspace_root,
            artifacts=spec.artifacts,
            artifact_states=artifact_states,
            spec_id=spec.id,
            graph_input=graph_input,
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
            failure_message = (
                trace.root.error.message
                if trace.root.error is not None
                else "DAG execution failed."
            )
            _mark_planned_artifacts_failed(
                dag_executor.artifact_states,
                failure_message,
            )
            trace.artifacts = dict(dag_executor.artifact_states)
        final_response = dag_run_fallback_message(record, trace)
        if spec.output is not None and trace.status == "completed":
            try:
                output_value = dag_executor.resolve_spec_output(spec.output, trace)
            except Exception as exc:
                trace.root.status = "failed"
                record.dag.status = "failed"
                final_response = f"Failed to resolve DAG output: {exc}"
            else:
                trace.root.value = output_value
                final_response = (
                    output_value
                    if isinstance(output_value, str)
                    else json.dumps(output_value, ensure_ascii=False)
                )
                trace.root.output = final_response
        _emit_dag(on_dag, record.dag)
        return _dag_loop_outcome(
            record=record,
            status="completed" if trace.status == "completed" else "failed",
            final_response=final_response,
            dag=record.dag,
            trace=trace,
            spec_id=spec.id,
            workspace_path=str(workspace),
        )

    async def resume_review(
        self,
        state: RunState,
        dag: DAG | None,
        *,
        approved: bool,
        review_level: ReviewLevel | None = None,
        feedback: str | None = None,
        messages: list[dict[str, Any]],
        build_user_message: Callable[..., dict[str, str]],
        dag_executor: DAGExecutor | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome | None:
        pending_review = state.pending_review
        if pending_review is None or pending_review.kind not in {"initial_dag", "dag_replan"}:
            return None
        record = state
        if record.dag is None:
            raise ValueError("DAG review state requires a DAG.")
        task_id = record.run_id
        if (
            record.dag.status == "completed"
            and record.trace is not None
            and record.pending_review is None
        ):
            trace = record.trace
            return _dag_loop_outcome(
                record=record,
                status="completed" if trace.status == "completed" else "failed",
                final_response=_trace_output(trace) or dag_run_fallback_message(record, trace),
                dag=record.dag,
                trace=trace,
            )
        if review_level is not None:
            record.review_level = review_level

        if not approved:
            record.pending_review = None
            observation = _format_dag_observation(
                kind="review_denied",
                task_id=task_id,
                record=record,
                review_message=_append_reviewer_feedback(
                    "Human reviewer denied the proposed DAG change. Continue without applying it.",
                    feedback,
                ),
            )
            outcome = await self._continue_from_observation(
                record,
                observation,
                messages=messages,
                build_user_message=build_user_message,
                dag_executor=dag_executor,
                on_token=on_token,
                on_event=on_event,
                on_dag=on_dag,
            )
            return outcome

        if dag is None:
            raise ValueError("Approved DAG review requires a submitted DAG.")

        submitted = dag.model_copy(deep=True)
        submitted.task_id = task_id
        submitted.dag_id = record.dag.dag_id
        submitted.version = record.dag.version + 1
        prepared = self.prepare_for_review(submitted, record.capability_scope.capability_ids)

        existing_node_ids = {node.id for node in record.dag.nodes}
        changed_node_ids = _changed_node_ids(record.dag, prepared)
        for node_id in changed_node_ids:
            if node_id in existing_node_ids:
                _invalidate_node_results(record, node_id)
        record.dag = prepared
        record.dag.status = "approved"
        record.dag_boundary_approved_version = record.dag.version
        record.pending_review = None
        _emit_dag(on_dag, record.dag)

        result = await self.execute(
            record,
            dag_executor=dag_executor,
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
        record: RunState,
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
    ) -> RunTrace | None:
        if record.pending_review is not None:
            raise DAGExecutionError("DAG is awaiting review and is not approved.")
        if entry_observation is not None and not replan:
            raise TypeError("entry_observation requires replan=True.")
        self._validate_dag_tools(record.dag, record.capability_scope.capability_ids)
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
                        approve_node_boundaries=(
                            record.dag_boundary_approved_version is not None
                            and record.dag_boundary_approved_version == record.dag.version
                        ),
                        skills=record.capability_scope.skills,
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
                        error_message=str(exc),
                        error_code=type(exc).__name__,
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
                        isinstance(_node_by_id(record.dag, nid).payload, StartNodePayload)
                        for nid in new_node_ids
                    ):
                        # Synthetic start nodes are bookkeeping, not an execution/replan turn.
                        cycle -= 1
                        continue
                    layer_error = ""

                pending_observation = _format_dag_observation(
                    kind="layer_failed" if layer_failed else ("dag_executed" if layer.status == "completed" else "layer_completed"),
                    task_id=record.run_id,
                    record=record,
                    last_error=layer_error if layer_failed else "",
                    failed_node_id=failed_node_id if layer_failed else None,
                )

            can_replan = replan and (record.dynamic_adjust or not _has_real_nodes(record.dag))
            if not can_replan:
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
                    task_id=record.run_id,
                    user_message=build_user_message(
                        prompt=pending_observation,
                        task_id=record.run_id,
                    ),
                    messages=messages,
                    allow_no_change=_has_real_nodes(record.dag),
                    model_step=cycle,
                    on_token=on_token,
                    on_event=on_event,
                    capability_scope=capability_scope_from_state(record.capability_scope),
                )
            except Exception as exc:
                pending_observation = _format_dag_observation(
                    kind="validation_error",
                    task_id=record.run_id,
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
                self._apply_replan(record, response)
            except Exception as exc:
                pending_observation = _format_dag_observation(
                    kind="validation_error",
                    task_id=record.run_id,
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
        record: RunState,
        observation: str,
        *,
        messages: list[dict[str, Any]],
        build_user_message: Callable[..., dict[str, str]],
        dag_executor: DAGExecutor | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        result = await self.execute(
            record,
            messages=messages,
            build_user_message=build_user_message,
            entry_observation=observation,
            dag_executor=dag_executor,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )
        return self._finalize_run(record, result)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _finalize_run(
        self,
        record: RunState,
        result: RunTrace | None,
    ) -> LoopOutcome:
        if record.pending_review is not None:
            return _dag_loop_outcome(
                record=record,
                status="awaiting_review",
                final_response=record.pending_review.message,
                dag=record.pending_review.proposed_dag,
                trace=result,
                pending_review=record.pending_review,
            )
        message = _trace_output(result) or dag_run_fallback_message(record, result)
        outcome_dag = record.dag if _has_real_nodes(record.dag) else None
        return _dag_loop_outcome(
            record=record,
            status="completed" if result.status == "completed" else "failed",
            final_response=message,
            dag=outcome_dag,
            trace=result,
        )

    def _absorb_partial_results(
        self,
        record: RunState,
        trace: RunTrace | None,
        on_dag: Callable[[DAG], None] | None = None,
        *,
        dag_executor: DAGExecutor | None = None,
        error_message: str | None = None,
        error_code: str = "",
    ) -> RunTrace:
        if trace is None:
            trace = _empty_run_trace(run_id=record.run_id, dag=record.dag)
        current_node_ids = _dag_node_ids(record.dag)
        active_executor = dag_executor or self.dag_executor
        for node_id, node_trace in active_executor.partial_node_traces.items():
            if node_id in current_node_ids:
                trace.root.upsert_child(node_trace)
                _node_by_id(record.dag, node_id).status = _node_status_from_trace(node_trace.status)
        for failed_id in _failed_node_ids_from_trace(trace, valid_node_ids=current_node_ids):
            _node_by_id(record.dag, failed_id).status = "failed"
        record.dag.status = "failed"
        trace.root.status = "failed"
        if error_message:
            trace.root.error = RunTraceError(message=error_message, code=error_code)
        record.trace = trace
        _emit_dag(on_dag, record.dag)
        return trace

    def _apply_replan(
        self,
        record: RunState,
        next_dag: DAG,
    ) -> None:
        is_initial = not _has_real_nodes(record.dag)
        prepared = next_dag.model_copy(deep=True)
        prepared.task_id = record.run_id
        prepared.dag_id = record.dag.dag_id
        prepared.version = record.dag.version + 1
        prepared = self.prepare_for_review(prepared, record.capability_scope.capability_ids)

        changed = _changed_node_ids(record.dag, prepared)
        previous_boundary_approved_version = record.dag_boundary_approved_version
        needs_review = bool(changed) and _review_policy(record.review_level).reviews_dag_changes()
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
        if changed:
            record.dag_boundary_approved_version = None
        elif previous_boundary_approved_version == record.dag.version - 1:
            record.dag_boundary_approved_version = record.dag.version
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
        record: RunState,
        trace: RunTrace | None,
        on_dag: Callable[[DAG], None] | None,
    ) -> RunTrace | None:
        if record.pending_review is not None and trace is None and record.trace is None:
            record.dag.status = "review_required"
            _emit_dag(on_dag, record.dag)
            return None
        trace = trace or record.trace or _empty_run_trace(run_id=record.run_id, dag=record.dag)
        node_traces = _node_traces_by_id(trace)
        dag_node_ids = _dag_node_ids(record.dag)
        all_nodes_completed = bool(dag_node_ids) and all(
            nid in node_traces and node_traces[nid].status in SETTLED_NODE_STATUSES
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

    def prepare_for_review(
        self,
        dag: DAG,
        capability_ids: Sequence[str] | None = None,
    ) -> DAG:
        prepared = self.dag_executor.normalize(dag)
        validate_dag(prepared)
        self._validate_dag_tools(prepared, capability_ids)
        return prepared

    def _validate_dag_tools(
        self,
        dag: DAG,
        capability_ids: Sequence[str] | None = None,
    ) -> None:
        unknown_tools = sorted({
            invocation.capability_id
            for invocation in iter_dag_invocations(dag.nodes)
            if invocation.capability_id
            and not self._is_enabled_capability(invocation.capability_id, capability_ids)
        })
        if unknown_tools:
            available_capabilities = [
                definition.id
                for definition in self.tool_adapter.capabilities(
                    self.enabled_toolsets,
                    capability_ids=capability_ids,
                )
            ]
            raise DAGValidationError(
                "Unknown capability(s): "
                f"{', '.join(unknown_tools)}. "
                "Available capabilities: "
                f"{', '.join(sorted(available_capabilities))}."
            )

    def _is_enabled_capability(
        self,
        capability_id: str,
        capability_ids: Sequence[str] | None = None,
    ) -> bool:
        try:
            self.tool_adapter.ensure_allowed(
                capability_id,
                enabled_toolsets=self.enabled_toolsets,
                capability_ids=capability_ids,
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
    run_id: str | None = None,
    model_step: int | None = None,
    on_token: Callable[[str], None] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> ChatResponse:
    if on_token is None and on_event is None:
        return await provider.chat(messages)

    stream = response_token_stream(
        on_raw=on_token,
        on_event=on_event,
        context=ResponseStreamContext.create(run_id=run_id, model_step=model_step),
    )
    if stream is None:
        return await provider.chat(messages)

    content = ""
    response: ChatResponse | None = None
    try:
        stream.start()
        if hasattr(provider, "stream_chat"):
            async for event in provider.stream_chat(messages):
                if event.type == "token" and event.content:
                    if getattr(event, "channel", "content") == "reasoning":
                        stream.emit_channel("reasoning", event.content)
                    else:
                        content += event.content
                        stream(event.content)
                elif event.type == "done":
                    response = event.response
        else:
            response = await provider.chat(messages)
    finally:
        stream.finish()
    return response or ChatResponse(content=content)


# ------------------------------------------------------------------
# Observation formatting
# ------------------------------------------------------------------

def _format_dag_observation(
    *,
    kind: str,
    task_id: str | None,
    record: RunState | None = None,
    last_error: str = "",
    failed_node_id: str | None = None,
    validation_error: str = "",
    review_message: str = "",
) -> str:
    sections = [
        f"DAG observation: {kind}",
        f"Task id: {task_id or (record.run_id if record else '<new>')}",
    ]
    if validation_error:
        sections.append(f"Validation error:\n{validation_error}")
    if review_message:
        sections.append(f"Review message:\n{review_message}")
    if record is None:
        return "\n\n".join(sections)

    completed = [
        f"- {node_id}: {context_excerpt(str(node_trace.output or '').strip(), limit=MAX_NODE_RESULT_CONTEXT_CHARS)}"
        for node_id, node_trace in _completed_node_traces(record).items()
    ]
    if completed:
        sections.append("Completed node outputs:\n" + "\n".join(completed))
    if failed_node_id:
        sections.append(f"Failed node: {failed_node_id}")
    if last_error:
        sections.append(f"Error:\n{last_error}")

    executions = _format_recent_capability_executions(record.trace, limit=6)
    if executions:
        sections.append("Node executions:\n" + "\n".join(executions))

    return context_excerpt(
        "\n\n".join(sections),
        limit=MAX_EXECUTION_CONTEXT_CHARS,
    )


def format_dag_execution_context(dag: DAG | None, trace: RunTrace | None) -> str:
    """Format DAG execution details for validation and fallback output."""
    lines: list[str] = []
    if dag:
        lines.append(f"DAG ({len(dag.nodes)} nodes, status={dag.status}):")
        for node in dag.nodes:
            if isinstance(node.payload, StartNodePayload):
                lines.append(f"  - {node.id}: internal start")
                continue
            if not isinstance(node.payload, CapabilityNodePayload):
                lines.append(f"  - {node.id}: {node.payload.type} node")
                continue
            invocation = node.payload.invocation
            lines.append(
                f"  - {node.id}: "
                f"{invocation.capability_id}({invocation.arguments})"
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
                context_excerpt(
                    str(node_trace.output or "").strip(),
                    limit=MAX_NODE_RESULT_CONTEXT_CHARS,
                )
                or (node_trace.error.message if node_trace.error else status)
            )
            lines.append(f"  - {node_id} ({status}): {response}")
    if not lines:
        return ""
    return context_excerpt(
        "\n".join(lines),
        limit=MAX_EXECUTION_CONTEXT_CHARS,
    )


def dag_run_fallback_message(record: RunState, trace: RunTrace) -> str:
    node_traces = _node_traces_by_id(trace)
    lines = [
        "DAG execution completed." if trace.status == "completed" else "DAG execution stopped before completion.",
        "",
        "Node results:",
    ]
    if not node_traces:
        if trace.root.error is not None:
            lines.append(f"- Run failed: {trace.root.error.message}")
        else:
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
    record: RunState,
    status: LoopStatus,
    final_response: str,
    dag: DAG | None = None,
    trace: RunTrace | None = None,
    spec_id: str | None = None,
    workspace_path: str | None = None,
    pending_review: PendingReview | None = None,
) -> LoopOutcome:
    state = record.model_copy(update={
        "status": status,
        "dag": dag,
        "trace": trace,
        "pending_review": pending_review,
        "pending_invocation": None,
        "spec_id": spec_id if spec_id is not None else record.spec_id,
        "workspace_path": workspace_path if workspace_path is not None else record.workspace_path,
    })
    return LoopOutcome(
        state=state,
        execution_context=format_dag_execution_context(dag, trace),
        output_text=final_response,
    )


def _strip_system_message(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if messages and messages[0].get("role") == "system":
        return [dict(message) for message in messages[1:]]
    return [dict(message) for message in messages]


def _last_user_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    raise ValueError("messages must contain at least one user message.")


def _messages_before_last_user(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return [dict(message) for message in messages[:index]]
    raise ValueError("messages must contain at least one user message.")


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


def _completed_node_traces(record: RunState) -> dict[str, RunTraceNode]:
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
    record: RunState,
    trace: RunTrace | None,
    output: str,
) -> RunTrace:
    trace = trace or record.trace or _empty_run_trace(run_id=record.run_id, dag=record.dag)
    trace.root.output = output
    record.trace = trace
    return trace


def _trace_output(trace: RunTrace) -> str:
    return str(trace.root.output or "").strip()


def _node_traces_by_id(trace: RunTrace | None) -> dict[str, RunTraceNode]:
    return {} if trace is None else trace.dag_node_traces()


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
                payload=StartNodePayload(type="start"),
            )
        ],
    )


def _has_real_nodes(dag: DAG) -> bool:
    return any(not isinstance(node.payload, StartNodePayload) for node in dag.nodes)


def _dag_completed(record: RunState) -> bool:
    trace = record.trace
    node_traces = _node_traces_by_id(trace) if trace is not None else {}
    return all(
        node.id in node_traces and node_traces[node.id].status in SETTLED_NODE_STATUSES
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


def _format_recent_capability_executions(trace: RunTrace | None, *, limit: int) -> list[str]:
    if trace is None:
        return []
    executions: list[str] = []

    def visit(node: RunTraceNode, current_node_id: str | None = None) -> None:
        next_node_id = node.ref.get("node_id") if node.kind == "dag_node" else current_node_id
        if node.kind == "capability_call" and node.capability_execution is not None:
            invocation = node.capability_execution.invocation
            result = node.capability_execution.result
            args = context_excerpt(
                json.dumps(invocation.arguments, ensure_ascii=False, sort_keys=True),
                limit=MAX_CAPABILITY_ARGS_CONTEXT_CHARS,
            )
            content = _capability_execution_content(node)
            detail = [
                f"- node: {next_node_id or '<tool>'}",
                f"  tool: {invocation.capability_id}",
                f"  args: {args}",
                f"  status: {node.status}",
            ]
            if content:
                detail.extend([
                    "  content:",
                    *_indent_block(content, prefix="    "),
                ])
            elif result is not None:
                detail.extend([
                    "  content:",
                    "    ",
                ])
            executions.append("\n".join(detail))
        for child in node.children:
            visit(child, next_node_id)

    visit(trace.root)
    return executions[-limit:]


def _capability_execution_content(node: RunTraceNode) -> str:
    parts: list[str] = []
    result = node.capability_execution.result if node.capability_execution else None
    if result is not None:
        parts.extend([
            result.content,
            result.error,
            result.stdout,
            result.stderr,
        ])
    parts.extend([
        str(node.output or ""),
        node.error.message if node.error else "",
    ])
    return context_excerpt(
        "\n".join(_unique_nonempty(parts)),
        limit=MAX_NODE_RESULT_CONTEXT_CHARS,
    )


def _unique_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _indent_block(text: str, *, prefix: str) -> list[str]:
    return [f"{prefix}{line}" if line else prefix.rstrip() for line in text.splitlines()]


def _invalidate_node_results(record: RunState, node_id: str) -> None:
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
