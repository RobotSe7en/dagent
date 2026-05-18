"""DAG agent loop."""

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
    DAGStepResult,
    LoopOutcome,
    LoopStatus,
    PendingReview,
    CapabilityDefinition,
    CapabilityInvocation,
    CapabilityExecutionRecord,
    TraceEvent,
)
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
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        return await self.loop.run(
            request,
            task_id=resolved_task_id,
            review_level=review_level,
            messages=self.messages,
            initial_user_message=self.build_request_user_message(
                prompt=request,
                task_id=resolved_task_id,
            ),
            build_user_message=self.build_request_user_message,
            runtime_mode=runtime_mode,
            force_review=force_review,
            on_token=on_token,
            on_trace=on_trace,
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
        on_trace: Callable[[TraceEvent], None] | None = None,
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
            on_trace=on_trace,
            on_event=on_event,
            on_dag=on_dag,
        )

    async def execute(
        self,
        record: RuntimeTaskRecord,
        *,
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
        max_cycles: int | None = None,
    ) -> DAGStepResult:
        return await self.loop.execute(
            record,
            messages=self.messages,
            build_user_message=self.build_request_user_message,
            on_token=on_token,
            on_trace=on_trace,
            on_event=on_event,
            on_dag=on_dag,
            max_cycles=max_cycles,
        )

    async def run_spec(
        self,
        spec: DAGSpec,
        *,
        workspace_root: str | Path = ".dagent-runs",
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        return await self.loop.run_spec(
            spec,
            workspace_root=workspace_root,
            on_token=on_token,
            on_trace=on_trace,
            on_event=on_event,
            on_dag=on_dag,
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

    async def run(
        self,
        request: str,
        *,
        task_id: str | None = None,
        review_level: ReviewLevel = "fast",
        messages: list[dict[str, Any]],
        initial_user_message: dict[str, str],
        build_user_message: Callable[..., dict[str, str]],
        runtime_mode: str = "auto",
        force_review: bool = False,
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
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
                    messages=messages,
                    user_message=(
                        initial_user_message
                        if cycles_used == 1
                        else build_user_message(prompt=planning_prompt, task_id=resolved_task_id)
                    ),
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
        )

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
            record,
            messages=messages,
            build_user_message=build_user_message,
            on_token=on_token,
            on_trace=on_trace,
            on_event=on_event,
            on_dag=on_dag,
            max_cycles=max(0, self.max_cycles - cycles_used),
        )
        return self._finalize_run(record, result)

    async def run_spec(
        self,
        spec: DAGSpec,
        *,
        workspace_root: str | Path = ".dagent-runs",
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
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
            artifact_states=artifact_states,
        )
        _emit_dag(on_dag, dag)
        dag_executor = DAGExecutor(
            capability_executor=self.dag_executor.capability_executor,
            workspace_path=workspace,
            artifacts=spec.artifacts,
            artifact_states=artifact_states,
            spec_id=spec.id,
        )

        status = "running"
        node_results = {}
        traces: list[TraceEvent] = []
        try:
            while True:
                step = await dag_executor.execute_next_ready_layer(
                    dag,
                    initial_results=node_results,
                    record_dag_start=not node_results,
                    on_trace=on_trace,
                    on_token=on_token,
                    on_event=on_event,
                )
                traces.extend(step.traces)
                node_results = step.node_results
                record.node_results = dict(step.node_results)
                record.execution_records = list(step.execution_records)
                record.require_dag_state().artifact_states = dict(step.artifact_states)
                _apply_step_result_to_dag(dag, step)
                if step.completed:
                    status = (
                        "failed"
                        if _has_required_artifact_failure(spec.artifacts, step.artifact_states)
                        else "completed"
                    )
                    dag.status = status
                    break
                if not step.node_results:
                    status = "failed"
                    dag.status = "failed"
                    break
        except Exception as exc:
            status = "failed"
            dag.status = "failed"
            traces.extend(dag_executor.trace_recorder.events)
            _absorb_spec_partial_results(record, dag_executor)
            _mark_planned_artifacts_failed(dag_executor.artifact_states, str(exc))
            record.require_dag_state().artifact_states = dict(dag_executor.artifact_states)

        record.status = status
        record.dag = dag
        result = DAGStepResult(
            dag_id=dag.dag_id,
            completed=status == "completed",
            node_results=dict(record.node_results),
            traces=traces,
            execution_records=list(record.execution_records),
            artifact_states=dict(record.artifact_states),
        )
        _emit_dag(on_dag, dag)
        return _dag_loop_outcome(
            status=status,  # type: ignore[arg-type]
            final_response=dag_run_fallback_message(record, result),
            dag=dag,
            dag_run=result,
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
        on_trace: Callable[[TraceEvent], None] | None = None,
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
                on_trace=on_trace,
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
                _invalidate_patch_results(record, node_id)
        record.dag = prepared
        record.dag.status = "approved"
        record.pending_review = None
        _emit_dag(on_dag, record.dag)

        result = await self.execute(
            record,
            messages=messages,
            build_user_message=build_user_message,
            on_token=on_token,
            on_trace=on_trace,
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
        messages: list[dict[str, Any]],
        build_user_message: Callable[..., dict[str, str]],
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
        max_cycles: int | None = None,
    ) -> DAGStepResult:
        if record.pending_review is not None:
            raise DAGExecutionError("DAG is awaiting review and is not approved.")
        self._validate_dag_tools(record.dag)

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
                        on_token=on_token,
                        on_event=on_event,
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
                    record.require_dag_state().artifact_states = dict(layer.artifact_states)
                    record.execution_records = self.dag_executor.execution_store.records_for_task(record.task_id)
                    if new_node_ids and all(nid == "start" for nid in new_node_ids):
                        # Synthetic start nodes are bookkeeping, not an execution/replan turn.
                        cycle -= 1
                        continue
                    layer_error = ""

                pending_observation = _format_dag_observation(
                    kind="layer_failed" if layer_failed else ("dag_executed" if layer.completed else "layer_completed"),
                    task_id=record.task_id,
                    record=record,
                    last_error=layer_error if layer_failed else "",
                    failed_node_id=failed_node_id if layer_failed else None,
                )

            # Ask LLM for the next step: continue, replan, or finish.
            try:
                response = await self._request_dag(
                    task_id=record.task_id,
                    user_message=build_user_message(
                        prompt=pending_observation,
                        task_id=record.task_id,
                    ),
                    messages=messages,
                    on_token=on_token,
                )
            except Exception as exc:
                traces.append(_dag_revision_trace_event(dag_id=record.dag.dag_id))
                pending_observation = _format_dag_observation(
                    kind="validation_error",
                    task_id=record.task_id,
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
                    record=record,
                    validation_error=str(exc),
                )
                continue
            traces.append(_dag_revision_trace_event(dag_id=record.dag.dag_id))
            _emit_dag(on_dag, record.dag)
            pending_observation = None

            # Review gate
            if record.pending_review is not None:
                break

        return self._finalize(record, traces, on_dag)

    async def _continue_from_observation(
        self,
        record: RuntimeTaskRecord,
        observation: str,
        *,
        messages: list[dict[str, Any]],
        build_user_message: Callable[..., dict[str, str]],
        on_token: Callable[[str], None] | None = None,
        on_trace: Callable[[TraceEvent], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
        extra_events: list[dict[str, Any]] | None = None,
    ) -> LoopOutcome:
        try:
            response = await self._request_dag(
                task_id=record.task_id,
                messages=messages,
                user_message=build_user_message(
                    prompt=observation,
                    task_id=record.task_id,
                ),
                on_token=on_token,
            )
        except Exception as exc:
            record.final_response = (
                "DAG review was denied, and the planner could not continue: "
                f"{type(exc).__name__}: {exc}"
            )
            result = self._finalize(record, [], on_dag)
            return self._finalize_run(record, result, extra_events=extra_events)

        if isinstance(response, str):
            record.final_response = response
            result = self._finalize(record, [], on_dag)
            return self._finalize_run(record, result, extra_events=extra_events)

        if response is None:
            result = self._finalize(record, [], on_dag)
            return self._finalize_run(record, result, extra_events=extra_events)

        self._apply_replan(record, response)
        _emit_dag(on_dag, record.dag)
        if record.pending_review is not None:
            return self._finalize_run(
                record,
                DAGStepResult(
                    dag_id=record.dag.dag_id,
                    completed=False,
                    node_results=dict(record.node_results),
                    execution_records=list(record.execution_records),
                ),
                extra_events=extra_events,
            )

        result = await self.execute(
            record,
            messages=messages,
            build_user_message=build_user_message,
            on_token=on_token,
            on_trace=on_trace,
            on_event=on_event,
            on_dag=on_dag,
        )
        return self._finalize_run(record, result, extra_events=extra_events)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _needs_review(self, record: RuntimeTaskRecord, *, force: bool = False) -> bool:
        if force:
            return True
        return review_policy(record.review_level).reviews_dag_changes()

    def _finalize_run(
        self,
        record: RuntimeTaskRecord,
        result: DAGStepResult,
        *,
        extra_events: list[dict[str, Any]] | None = None,
    ) -> LoopOutcome:
        if record.pending_review is not None:
            return _dag_loop_outcome(
                status="awaiting_review",
                final_response=record.pending_review.message,
                dag=record.pending_review.proposed_dag,
                dag_run=result,
                task_id=record.task_id,
                pending_review=record.pending_review,
                extra_events=extra_events,
            )
        message = record.final_response or dag_run_fallback_message(record, result)
        return _dag_loop_outcome(
            status="completed" if result.completed else "failed",
            final_response=message,
            dag=record.dag,
            dag_run=result,
            task_id=record.task_id,
            extra_events=extra_events,
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

        # Remove stale results for changed/deleted nodes and their downstream.
        # This must use the old DAG before replacement so downstream invalidation
        # follows the graph that produced the cached results.
        old_node_ids = {node.id for node in record.dag.nodes}
        for node_id in changed:
            if node_id in old_node_ids:
                _invalidate_patch_results(record, node_id)

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
    ) -> DAGStepResult:
        record.execution_records = self.dag_executor.execution_store.records_for_task(record.task_id)
        all_nodes_completed = all(
            nid in record.node_results and record.node_results[nid].completed
            for nid in _dag_node_ids(record.dag)
        )
        completed = record.dag.status != "failed" and (
            bool(record.final_response) or all_nodes_completed
        )
        result = DAGStepResult(
            dag_id=record.dag.dag_id,
            completed=completed,
            node_results=dict(record.node_results),
            traces=traces,
            execution_records=list(record.execution_records),
            artifact_states=dict(record.artifact_states),
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
        return result

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
        f"- {execution.node_id} ({execution.invocation.capability_id}): {execution.status}"
        + (f" error={execution.error}" if execution.error else "")
        for execution in record.execution_records[-6:]
    ]
    if recent:
        sections.append("Recent capability executions:\n" + "\n".join(recent))

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


def format_dag_execution_context(dag: DAG | None, dag_run: DAGStepResult | None) -> str:
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


def dag_run_fallback_message(record: RuntimeTaskRecord, result: DAGStepResult) -> str:
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
    dag_run: DAGStepResult | None = None,
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
        execution_context=format_dag_execution_context(dag, dag_run),
        final_answer=final_response,
        events=events,
        invocations=invocations,
        dag=dag,
        dag_run=dag_run,
        task_id=task_id,
        spec_id=spec_id,
        workspace_path=workspace_path,
        pending_review=pending_review,
        artifact_states=dict(dag_run.artifact_states) if dag_run is not None else {},
    )


def _tool_name_from_capability(capability_id: str) -> str:
    return capability_id.removeprefix("tool.")


def _emit_dag(on_dag: Callable[[DAG], None] | None, dag: DAG) -> None:
    if on_dag is not None:
        on_dag(dag.model_copy(deep=True))


def _apply_step_result_to_dag(dag: DAG, step: DAGStepResult) -> None:
    nodes_by_id = {node.id: node for node in dag.nodes}
    for node_id, node_result in step.node_results.items():
        node = nodes_by_id.get(node_id)
        if node is not None:
            node.status = "completed" if node_result.completed else "failed"
    dag.status = "completed" if step.completed else "running"


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


def _absorb_spec_partial_results(
    record: RuntimeTaskRecord,
    dag_executor: DAGExecutor,
) -> None:
    current_node_ids = _dag_node_ids(record.dag)
    for node_id, node_result in dag_executor.partial_node_results.items():
        if node_id in current_node_ids:
            record.node_results[node_id] = node_result
            _node_by_id(record.dag, node_id).status = (
                "completed" if node_result.completed else "failed"
            )
    record.execution_records = dag_executor.execution_store.records_for_task(record.task_id)
    for failed_id in _failed_node_ids(record.execution_records, valid_node_ids=current_node_ids):
        _node_by_id(record.dag, failed_id).status = "failed"


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
    records: list[CapabilityExecutionRecord],
    *,
    valid_node_ids: set[str] | None = None,
) -> str | None:
    for record in reversed(records):
        if record.status == "failed" and (valid_node_ids is None or record.node_id in valid_node_ids):
            return record.node_id
    return None


def _failed_node_ids(
    records: list[CapabilityExecutionRecord],
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
