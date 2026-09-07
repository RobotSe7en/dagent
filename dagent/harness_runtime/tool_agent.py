"""Bounded tool-using agent loop."""

from __future__ import annotations
from dagent.harness_runtime.context import compaction_source

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from uuid import uuid4

from dagent.capabilities.toolsets import CapabilityToolAdapter
from dagent.capabilities.providers import check_tool_boundary_for_review
from dagent.capabilities.workspace import current_workspace_root
from dagent.harness_runtime.dag_builder import (
    MAX_EXECUTION_CONTEXT_CHARS,
    context_excerpt,
)
from dagent.harness_runtime.capability_executor import (
    CapabilityExecutionCallbacks,
    CapabilityExecutionContext,
    CapabilityExecutor,
)
from dagent.harness_runtime.capability_scope import (
    CapabilityScope,
    DEFAULT_CAPABILITY_SCOPE,
    capability_scope_from_state,
    capability_scope_to_state,
)
from dagent.harness_runtime.execution_usage import record_model_turn
from dagent.harness_runtime.runtime_events import (
    LoopEventHandler,
    ResponseStreamContext,
    TokenHandler,
    response_token_stream,
)
from dagent.harness_runtime.steering import (
    QueuedSteer,
    current_run_steering_control,
)
from dagent.harness_runtime.llm_retry import (
    DEFAULT_LLM_RETRY_POLICY,
    LLMRetryPolicy,
    LLMRetrySleep,
    run_with_llm_retries,
)
from dagent.review import CapabilityReviewDecision, ReviewDecision, ReviewLevel, _append_reviewer_feedback, _review_policy
from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider, ChatResponse, ToolCall
from dagent.providers.base import normalize_chat_response
from dagent.providers.model_io import (
    ModelRequest,
    chat_response_from_model,
    complete_model,
    stream_model,
)
from dagent.schemas import (
    AssistantMessage,
    Boundary,
    LoopOutcome,
    PendingReview,
    CapabilityDefinition,
    CapabilityInvocation,
    CapabilityResult,
    ContentReference,
    ContextPolicy,
    ContextSummary,
    ContextUsage,
    ConversationItem,
    ConversationState,
    ResultStoragePolicy,
    RunState,
    RunTrace,
    RunTraceError,
    RunTraceNode,
    ToolCallItem,
    ToolResultMessage,
    UserMessage,
)
from dagent.state import PromptBuilder, PromptRequest
from dagent.state.prompt_builder import PromptSkill
from dagent.harness_runtime.context import ContextAssembler
from dagent.harness_runtime.result_storage import normalize_capability_result, ResultStore, ResultStorageError
from dagent.schemas.retention import ResultRetention
from dagent.schemas.conversation import (
    StoredContent,
    ToolResultStatus,
    inline_content,
    stored_content_text,
)
from dagent.schemas.common import validate_runtime_directory
from dagent.schemas.results import PendingCapabilityReviewItem, _PendingToolBatch, _PendingToolCall


class ToolResultStorageFailure(RuntimeError):
    """Carry the stopped loop snapshot to the public Runner boundary."""

    def __init__(self, cause: ResultStorageError, outcome: LoopOutcome) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.outcome = outcome


def _record_storage_failure(
    trace: RunTrace, invocation: CapabilityInvocation, error: ResultStorageError,
) -> None:
    node = _find_capability_node(trace.root, invocation.invocation_id)
    if node is None:
        node = RunTraceNode.capability_call(
            parent_id=trace.root.id,
            invocation=invocation,
            result=error.audit_result,
        )
        trace.root.children.append(node)
    else:
        assert node.capability_execution is not None
        node.capability_execution.result = error.audit_result
    node.status = "failed"
    node.ended_at = datetime.now(timezone.utc)
    node.error = RunTraceError(message=str(error), code="ResultStorageError")
    trace.root.status = "failed"
    trace.root.error = node.error.model_copy()
    trace.root.ended_at = datetime.now(timezone.utc)


MAX_TOOL_RESULT_CONTEXT_CHARS = 4000
_STEER_SKIPPED_TOOL_CONTENT = (
    "[TOOL_SKIPPED] Not executed because the user steered the active run before "
    "this tool call started. Reconsider the call against the latest user guidance."
)
_STEER_STEP_LIMIT_FAILURE_DIAGNOSTIC = (
    "The task could not apply a user steering update because the maximum model "
    "steps were exhausted. The preceding assistant response was discarded."
)


SkillPromptResolver = Callable[[tuple[str, ...] | None], Sequence[PromptSkill]]


class ToolAgent:
    """Profile-backed tool agent facade used by the runtime."""

    def __init__(
        self,
        *,
        loop: "ToolAgentLoop",
        profile: AgentProfile,
        prompt_builder: PromptBuilder | None = None,
        max_steps: int = 888,
        extra_system_prompt: str | None = None,
        skill_prompt_resolver: SkillPromptResolver | None = None,
        context_policy: ContextPolicy | None = None,
        result_storage_policy: ResultStoragePolicy | None = None,
        context_assembler: ContextAssembler | None = None,
        prompt_context: str = "",
    ) -> None:
        self.loop = loop
        self.profile = profile
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.max_steps = max_steps
        self.extra_system_prompt = extra_system_prompt
        self.skill_prompt_resolver = skill_prompt_resolver
        self.context_policy = context_policy or ContextPolicy()
        self.result_storage_policy = result_storage_policy or ResultStoragePolicy()
        self.context_assembler = context_assembler or ContextAssembler(
            context_window_tokens=getattr(
                loop.provider,
                "configured_context_window_tokens",
                getattr(loop.provider, "context_window_tokens", None),
            ),
            max_output_tokens=getattr(loop.provider, "max_output_tokens", None),
            model_context_window_tokens=getattr(
                loop.provider, "model_context_window_tokens", None,
            ),
            request_token_counter=getattr(loop.provider, "count_tokens", None),
            request_reasoning_field=getattr(
                loop.provider,
                "context_reasoning_field",
                None,
            ),
        )
        self.prompt_context = prompt_context
        self.tools = self.loop.available_capabilities()
        self.system_message = self._build_system_message(DEFAULT_CAPABILITY_SCOPE)
        self.conversation = ConversationState()
        self.trace: RunTrace | None = None

    def _build_system_message(
        self,
        capability_scope: CapabilityScope,
        *,
        workspace_path: str | Path | None = None,
    ) -> dict[str, str]:
        skills = (
            []
            if self.skill_prompt_resolver is None
            else list(self.skill_prompt_resolver(capability_scope.skills))
        )
        return self.prompt_builder.build_system_message(
            PromptRequest(
                profile=self.profile,
                task_content="",
                skills=skills,
                context=self.prompt_context,
                extra_system_prompt=self.extra_system_prompt,
                workspace_path=workspace_path,
            )
        )

    async def run(
        self,
        message: str,
        *,
        run_id: str | None = None,
        review_level: ReviewLevel | None = "fast",
        capability_context: CapabilityExecutionContext | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> LoopOutcome:
        """Run one user turn from a new conversation."""
        boundary = Boundary(allowed_paths=["."])
        return await self.run_conversation(
            ConversationState(items=(UserMessage(content=message, run_id=run_id),)),
            run_id=run_id,
            review_level=review_level,
            capability_context=capability_context,
            capability_scope=capability_scope,
            boundary=boundary,
            on_token=on_token,
            on_event=on_event,
        )

    async def run_conversation(
        self,
        conversation: ConversationState,
        *,
        run_id: str | None = None,
        review_level: ReviewLevel | None = "fast",
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        boundary: Boundary | None = None,
        capability_context: CapabilityExecutionContext | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> LoopOutcome:
        """Run a provider-neutral conversation state."""
        self.conversation = conversation
        return await self._continue_conversation(
            conversation,
            run_id=run_id,
            review_level=review_level,
            boundary=boundary or Boundary(allowed_paths=["."]),
            capability_scope=capability_scope,
            capability_context=capability_context,
            on_token=on_token,
            on_event=on_event,
        )

    async def resume_review(
        self,
        state: RunState,
        *,
        approved: bool | None = None,
        capability_decisions: tuple[CapabilityReviewDecision, ...] = (),
        feedback: str | None = None,
        capability_context: CapabilityExecutionContext | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> LoopOutcome | None:
        """Apply a complete review decision and drain the saved tool round first."""
        pending = state.pending_review
        if pending is None or pending.kind != "capability_review" or state.pending_tool_batch is None:
            return None
        scope = capability_scope_from_state(state.capability_scope)
        for item in state.pending_tool_batch.calls:
            if item.invocation is None:
                continue
            definition = self.loop.tool_adapter.ensure_allowed(
                item.invocation.capability_id, enabled_toolsets=self.loop.enabled_toolsets,
                capability_ids=scope.capability_ids,
            )
            if item.call.name != self.loop.tool_adapter.function_name(definition):
                raise ValueError("Pending capability review tool name does not match its invocation.")
        decision = ReviewDecision(
            review_id=pending.review_id, approved=approved,
            capability_decisions=capability_decisions, feedback=feedback,
        )
        decisions = decision.decisions_for(pending)
        restored = state.model_copy(deep=True)
        batch = restored.pending_tool_batch
        assert batch is not None
        expected = tuple(item.review for item in batch.calls[batch.next_index:]
                         if item.review is not None and item.approved is None)
        if pending.capability_items != expected:
            raise ValueError("Pending capability review does not match its invocation batch.")
        for item in batch.calls[batch.next_index:]:
            if item.call.id in decisions:
                item.approved = decisions[item.call.id]
                item.feedback = feedback
        conversation = restored.model_thread or restored.conversation
        if conversation is None:
            raise ValueError("Tool review checkpoint has no model thread.")
        scope = capability_scope_from_state(restored.capability_scope)
        return await self._continue_conversation(
            conversation, run_id=restored.run_id, review_level=restored.review_level,
            boundary=batch.boundary, capability_scope=scope,
            capability_context=replace(
                capability_context or CapabilityExecutionContext(task_id=restored.run_id),
                task_id=restored.run_id, workspace_path=restored.workspace_path,
                skills=scope.skills, extra_system_prompt=self.extra_system_prompt,
            ),
            on_token=on_token, on_event=on_event, resume_state=restored,
        )

    async def _continue_conversation(
        self,
        conversation: ConversationState,
        *,
        run_id: str | None = None,
        review_level: ReviewLevel | None,
        boundary: Boundary,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        capability_context: CapabilityExecutionContext | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
        resume_state: RunState | None = None,
    ) -> LoopOutcome:
        """Resume a tool-agent conversation from typed state."""
        if capability_context is not None:
            capability_context = replace(
                capability_context,
                extra_system_prompt=self.extra_system_prompt,
            )
        system_message = self._build_system_message(
            capability_scope,
            workspace_path=(
                capability_context.workspace_path
                if capability_context is not None
                and capability_context.workspace_path is not None
                else current_workspace_root(
                    self.loop.capability_executor.workspace_root
                )
            ),
        )
        failure: ToolResultStorageFailure | None = None
        try:
            outcome = await self.loop.run(
                conversation,
                run_id=run_id,
                boundary=boundary,
                max_steps=self.max_steps,
                system_message=system_message,
                context_policy=self.context_policy,
                result_storage_policy=self.result_storage_policy,
                context_assembler=self.context_assembler,
                resume_state=resume_state,
                review_level=review_level,
                capability_ids=capability_scope.capability_ids,
                capability_scope=capability_scope,
                capability_context=capability_context,
                skills=capability_scope.skills,
                on_token=on_token,
                on_event=on_event,
            )
        except ToolResultStorageFailure as exc:
            failure = exc
            outcome = exc.outcome

        if review_level is not None:
            outcome = outcome.model_copy(
                update={
                    "state": outcome.state.model_copy(
                        update={"review_level": review_level}
                    )
                }
            )
        self.conversation = outcome.state.model_thread or conversation
        self.trace = outcome.state.trace
        if failure is not None:
            failure.outcome = outcome
            raise failure
        return outcome

    def reviewable_tool_names(self, capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE) -> set[str]:
        return self.loop.reviewable_tool_names(capability_scope.capability_ids)


class ToolAgentLoop:
    """Runs one bounded tool-using agent loop."""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        capability_executor: CapabilityExecutor,
        tool_adapter: CapabilityToolAdapter,
        runtime_directory: str,
        enabled_toolsets: Sequence[str] = ("builtin",),
        _llm_retry_policy: LLMRetryPolicy = DEFAULT_LLM_RETRY_POLICY,
        _llm_retry_sleep: LLMRetrySleep = asyncio.sleep,
    ) -> None:
        self.provider = provider
        self.capability_executor = capability_executor
        self.tool_adapter = tool_adapter
        self.runtime_directory = validate_runtime_directory(runtime_directory)
        self.enabled_toolsets = tuple(enabled_toolsets)
        self.llm_retry_policy = _llm_retry_policy
        self.llm_retry_sleep = _llm_retry_sleep

    def available_capabilities(
        self,
        capability_ids: Sequence[str] | None = None,
    ) -> list[CapabilityDefinition]:
        return self.tool_adapter.capabilities(
            self.enabled_toolsets,
            capability_ids=capability_ids,
        )

    def reviewable_tool_names(self, capability_ids: Sequence[str] | None = None) -> set[str]:
        return self.tool_adapter.reviewable_names(
            self.enabled_toolsets,
            capability_ids=capability_ids,
        )

    def _preflight_call(
        self, call: ToolCallItem, boundary: Boundary,
        review_level: ReviewLevel | None, capability_ids: Sequence[str] | None,
        workspace: str | Path,
    ) -> _PendingToolCall:
        """Resolve and review a call without executing its handler."""
        tool_call = ToolCall(id=call.id, name=call.name, arguments=call.arguments)
        try:
            invocation = self.tool_adapter.invocation_from_tool_call(
                tool_call, boundary, enabled_toolsets=self.enabled_toolsets,
                capability_ids=capability_ids,
            )
        except KeyError as exc:
            return _PendingToolCall(call=call, error=f"[TOOL_ERROR] {_exception_message(exc)}")
        definition = self.tool_adapter.definition_from_tool_call(
            tool_call, enabled_toolsets=self.enabled_toolsets, capability_ids=capability_ids,
        )
        item = _PendingToolCall(call=call, invocation=invocation)
        result, paths = check_tool_boundary_for_review(definition, invocation, Path(workspace))
        if result is not None and not _reviewable_boundary_result(result):
            item.error = _tool_content(result)
            item.error_result = result
        elif review_level == "careful" and (
            result is not None or _review_policy(review_level).reviews_tool(definition.policy.risk)
        ):
            is_boundary = result is not None
            item.review = PendingCapabilityReviewItem(
                invocation_id=call.id, capability_id=invocation.capability_id,
                tool_name=call.name, arguments=call.arguments, risk=definition.policy.risk,
                reason="boundary_violation" if is_boundary else "risk",
                boundary_paths=paths, error=None if result is None else result.error or result.content,
                message=(f"Review boundary override: {invocation.capability_id}" if is_boundary
                         else f"Review capability call: {invocation.capability_id}"),
            )
        return item

    async def run(
        self,
        conversation: ConversationState,
        *,
        run_id: str | None = None,
        boundary: Boundary,
        max_steps: int = 888,
        system_message: dict[str, Any],
        context_policy: ContextPolicy,
        result_storage_policy: ResultStoragePolicy,
        context_assembler: ContextAssembler,
        resume_state: RunState | None = None,
        review_level: ReviewLevel | None = None,
        capability_ids: Sequence[str] | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
        skills: tuple[str, ...] | None = None,
        capability_context: CapabilityExecutionContext | None = None,
    ) -> LoopOutcome:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1.")

        loop_conversation = conversation
        new_items: list[ConversationItem] = []
        context_usages: list[ContextUsage] = list(resume_state.context_usage) if resume_state else []
        failure_diagnostic: str | None = None
        resolved_run_id = run_id or f"tool_run_{uuid4().hex}"
        steering = current_run_steering_control(resolved_run_id)
        trace = (resume_state.trace.model_copy(deep=True) if resume_state and resume_state.trace
                 else RunTrace(run_id=resolved_run_id, root=RunTraceNode.run(run_id=resolved_run_id)))
        execution_context = _execution_context(
            capability_context,
            task_id=resolved_run_id,
            skills=skills,
        )
        execution_context = replace(execution_context, runtime_directory=self.runtime_directory,
                                    max_shell_output_bytes=result_storage_policy.max_shell_output_bytes)
        state_scope = _state_capability_scope(
            capability_scope,
            capability_ids=capability_ids,
            skills=skills,
        )
        capability_callbacks = CapabilityExecutionCallbacks(on_token=on_token, on_event=on_event)
        workspace = execution_context.workspace_path or current_workspace_root(self.capability_executor.workspace_root)

        def apply_steers(steers: tuple[QueuedSteer, ...]) -> None:
            nonlocal loop_conversation
            for steer in steers:
                item = UserMessage(
                    id=steer.steer_id,
                    run_id=resolved_run_id,
                    content=steer.content,
                )
                loop_conversation = _append_item(loop_conversation, item)
                new_items.append(item)
            if steering is not None:
                steering.emit_applied(resolved_run_id, steers)

        def append_skipped(tool_calls: Sequence[ToolCall]) -> None:
            nonlocal loop_conversation
            loop_conversation, skipped_items = _append_skipped_tool_results(
                loop_conversation,
                tool_calls,
                run_id=resolved_run_id,
                content=_STEER_SKIPPED_TOOL_CONTENT,
            )
            new_items.extend(skipped_items)
            for call in tool_calls:
                node = _find_capability_node(trace.root, call.id)
                if node is not None:
                    node.status = "skipped"
                    node.ended_at = datetime.now(timezone.utc)

        def failed_outcome() -> LoopOutcome:
            trace.root.status = "failed"
            return LoopOutcome(
                state=_tool_run_state(
                    run_id=resolved_run_id,
                    status="failed",
                    conversation=loop_conversation,
                    trace=trace,
                    context_usage=context_usages,
                    capability_scope=state_scope,
                    workspace_path=(
                        None
                        if execution_context.workspace_path is None
                        else str(execution_context.workspace_path)
                    ),
                ),
                execution_context=(
                    failure_diagnostic
                    or _format_capability_execution_context(loop_conversation)
                ),
                new_items=tuple(new_items),
            )

        def handle_pending_steers(
            steers: tuple[QueuedSteer, ...],
            *,
            step: int,
            skipped_calls: Sequence[ToolCall] = (),
        ) -> bool:
            """Apply steers and continue, or discard them at the hard step limit."""

            nonlocal failure_diagnostic

            if not steers:
                return False
            if skipped_calls:
                append_skipped(skipped_calls)
            if step >= max_steps:
                if steering is not None:
                    steering.set_phase(resolved_run_id, "finishing")
                    steering.emit_discarded(
                        resolved_run_id,
                        steers,
                        "step_limit_exhausted",
                    )
                failure_diagnostic = _STEER_STEP_LIMIT_FAILURE_DIAGNOSTIC
                return False
            apply_steers(steers)
            return True

        async def process_batch(batch: _PendingToolBatch, *, step: int) -> LoopOutcome | None:
            nonlocal loop_conversation, boundary

            def pending_outcome() -> LoopOutcome:
                requirements = tuple(item.review for item in batch.calls[batch.next_index:]
                                     if item.review is not None and item.approved is None)
                single = requirements[0] if len(requirements) == 1 else None
                pending = PendingReview(
                    review_id=f"review_{uuid4().hex}", kind="capability_review",
                    message=single.message if single else f"Review {len(requirements)} capability calls",
                    capability_call=single.call() if single else None,
                    capability_calls=() if single else requirements,
                    payload=single.single_payload() if single else {},
                    queued_call_count=len(batch.calls) - batch.next_index - len(requirements),
                )
                trace.root.status = "awaiting_review"
                for item in batch.calls[batch.next_index:]:
                    if item.invocation is None:
                        continue
                    node = _find_capability_node(trace.root, item.call.id)
                    if node is None:
                        node = RunTraceNode.capability_call(
                            parent_id=trace.root.id, invocation=item.invocation,
                            status="awaiting_review" if item.review and item.approved is None else "planned",
                        )
                        trace.root.children.append(node)
                        if item.review is not None and not item.started:
                            self._emit_capability_event(on_event, item.invocation, "capability_call", run_id=resolved_run_id)
                            item.started = True
                    else:
                        node.status = "awaiting_review" if item.review and item.approved is None else "planned"
                return LoopOutcome(
                    state=_tool_run_state(
                        run_id=resolved_run_id, status="awaiting_review", conversation=loop_conversation,
                        trace=trace, context_usage=context_usages, pending_review=pending,
                        pending_tool_batch=batch, capability_scope=state_scope,
                        workspace_path=None if execution_context.workspace_path is None else str(execution_context.workspace_path),
                    ),
                    execution_context=_format_capability_execution_context(loop_conversation),
                    new_items=tuple(new_items),
                )

            unresolved = any(item.review is not None and item.approved is None
                             for item in batch.calls[batch.next_index:])
            if unresolved:
                pending_steers = (() if steering is None else
                                  steering.drain_or_transition(resolved_run_id, "awaiting_review"))
                if pending_steers:
                    remaining = [ToolCall(id=i.call.id, name=i.call.name, arguments=i.call.arguments)
                                 for i in batch.calls[batch.next_index:]]
                    if handle_pending_steers(pending_steers, step=step, skipped_calls=remaining):
                        return None
                    return failed_outcome()
                return pending_outcome()

            trace.root.status = "running"
            while batch.next_index < len(batch.calls):
                item = batch.calls[batch.next_index]
                tool_call = ToolCall(id=item.call.id, name=item.call.name, arguments=item.call.arguments)
                pending_steers = () if steering is None else steering.drain(resolved_run_id)
                if pending_steers:
                    remaining = [ToolCall(id=i.call.id, name=i.call.name, arguments=i.call.arguments)
                                 for i in batch.calls[batch.next_index:]]
                    if handle_pending_steers(pending_steers, step=step, skipped_calls=remaining):
                        return None
                    return failed_outcome()

                denied = item.approved is False
                if not denied:
                    if item.approved is True and item.review and item.review.reason == "boundary_violation":
                        batch.boundary = _boundary_with_reviewed_paths(batch.boundary, item.review.single_payload())
                        boundary = batch.boundary
                    fresh = self._preflight_call(item.call, batch.boundary, review_level, capability_ids, workspace)
                    if fresh.review is not None and (
                        item.approved is not True or fresh.review.reason == "boundary_violation"
                    ):
                        item.review = fresh.review
                        item.approved = None
                        item.invocation = fresh.invocation
                        return await process_batch(batch, step=step)
                    item.invocation = fresh.invocation
                    item.error = fresh.error
                    item.error_result = fresh.error_result

                invocation = item.invocation
                node = None if invocation is None else _find_capability_node(trace.root, item.call.id)
                if invocation is not None and node is None:
                    node = RunTraceNode.capability_call(parent_id=trace.root.id, invocation=invocation, status="running")
                    trace.root.children.append(node)
                if invocation is not None and node is not None:
                    node.status = "running"
                    if not item.started:
                        self._emit_capability_event(on_event, invocation, "capability_call", run_id=resolved_run_id)
                        item.started = True
                result = item.error_result
                stored_content = None
                normalized = None
                content = item.error or ""
                if denied:
                    content = "[DENIED] Human reviewer denied this tool call. Continue without executing it."
                elif invocation is not None and item.error is None:
                    call_context = execution_context
                    definition = self.tool_adapter.ensure_allowed(
                        invocation.capability_id, enabled_toolsets=self.enabled_toolsets, capability_ids=capability_ids,
                    )
                    boundary_result, _ = check_tool_boundary_for_review(definition, invocation, Path(workspace))
                    if boundary_result is not None and _reviewable_boundary_result(boundary_result) and review_level == "fast":
                        call_context = replace(execution_context, approved_boundary_invocation_id=invocation.invocation_id)
                    try:
                        result = await self.capability_executor.execute(invocation, context=call_context, callbacks=capability_callbacks)
                    except Exception as exc:
                        result = CapabilityResult.failed(invocation, str(exc), stop_reason=type(exc).__name__)
                    content = _tool_content(result)
                if result is not None:
                    try:
                        normalized = normalize_capability_result(
                            result, workspace_path=workspace, runtime_directory=self.runtime_directory,
                            policy=result_storage_policy, on_event=on_event,
                        )
                    except ResultStorageError as exc:
                        assert invocation is not None
                        _record_storage_failure(trace, invocation, exc)
                        raise ToolResultStorageFailure(exc, failed_outcome()) from exc
                    result = normalized.result
                    stored_content = normalized.content
                    content = stored_content_text(stored_content)
                content = _append_reviewer_feedback(content, item.feedback)
                if item.feedback and stored_content is not None:
                    stored_content = (stored_content.model_copy(update={"preview": content})
                                      if isinstance(stored_content, ContentReference) else inline_content(content))
                status = "denied" if denied else "completed" if result is not None and result.status == "completed" else "failed"
                tool_item = _tool_result_item(
                    tool_call=tool_call, run_id=resolved_run_id, status=status, content=content,
                    capability_id=None if invocation is None else invocation.capability_id,
                    stored_content=stored_content, retention=None if result is None else result.retention,
                    value=None if result is None else result.value,
                    value_reference=None if normalized is None else normalized.value_reference,
                    artifacts=() if normalized is None else normalized.references,
                )
                loop_conversation = _append_item(loop_conversation, tool_item)
                new_items.append(tool_item)
                if node is not None and invocation is not None:
                    node.status = "completed" if status == "completed" else "failed"
                    node.ended_at = datetime.now(timezone.utc)
                    if node.capability_execution is not None:
                        node.capability_execution.invocation = invocation
                        node.capability_execution.result = result
                    if denied:
                        node.error = RunTraceError(message="Human reviewer denied this capability call.", code="review_denied")
                    elif result is not None and result.error:
                        node.error = RunTraceError(message=result.error, code=result.stop_reason)
                    self._emit_capability_event(
                        on_event, invocation, "capability_result" if status == "completed" else "capability_error",
                        run_id=resolved_run_id, content=content,
                    )
                batch.next_index += 1
                boundary = batch.boundary
            return None

        if resume_state is not None and resume_state.pending_tool_batch is not None:
            resumed = await process_batch(resume_state.pending_tool_batch, step=0)
            if resumed is not None:
                return resumed

        for step in range(1, max_steps + 1):
            tool_definitions = self._llm_tool_definitions(capability_ids=capability_ids)
            while True:
                if steering is not None:
                    apply_steers(steering.drain(resolved_run_id))
                previous_revision = loop_conversation.revision
                prepared = await context_assembler.prepare(
                    result_store=ResultStore(
                        execution_context.workspace_path or current_workspace_root(self.capability_executor.workspace_root),
                        self.runtime_directory, on_event, read_boundary=boundary,
                    ),
                    system_message=system_message,
                    conversation=loop_conversation,
                    tools=tool_definitions,
                    policy=context_policy,
                    active_run_id=resolved_run_id,
                    compact=lambda summary, items, limit: self._compact_history(
                        summary,
                        items,
                        limit,
                        run_id=resolved_run_id,
                        on_event=on_event,
                        context_assembler=context_assembler,
                        context_policy=context_policy,
                    ),
                    stream=on_token is not None or on_event is not None,
                )
                loop_conversation = prepared.conversation
                updated_items = {item.id: item for item in loop_conversation.items}
                new_items[:] = [updated_items.get(item.id, item) for item in new_items]
                result_items = {item.call_id: item for item in loop_conversation.items
                                if isinstance(item, ToolResultMessage)}
                for node in trace.root.children:
                    execution = node.capability_execution
                    if execution is None or execution.result is None:
                        continue
                    item = result_items.get(execution.invocation.invocation_id)
                    if item is not None:
                        execution.result = execution.result.model_copy(update={
                            "retention": item.retention,
                            "content_reference": item.content if isinstance(item.content, ContentReference) else None,
                        })
                context_usages.append(prepared.usage)
                if on_event is not None and loop_conversation.revision != previous_revision:
                    on_event(
                        {
                            "type": "context_compacted",
                            "run_id": resolved_run_id,
                            "scope": "conversation",
                            "usage": prepared.usage.model_dump(mode="json"),
                        }
                    )
                late_steers = (
                    () if steering is None else steering.drain(resolved_run_id)
                )
                if not late_steers:
                    break
                apply_steers(late_steers)
            response = await self._chat(
                prepared.request,
                run_id=resolved_run_id,
                step=step,
                on_token=on_token,
                on_event=on_event,
            )

            assistant_message = self._assistant_message(response)
            assistant_message = assistant_message.model_copy(
                update={"run_id": resolved_run_id}
            )
            loop_conversation = _append_item(loop_conversation, assistant_message)
            new_items.append(assistant_message)
            trace.root.children.append(
                RunTraceNode(
                    parent_id=trace.root.id,
                    kind="model_call",
                    status="completed",
                    label=f"model_step_{step}",
                    ref={"step": str(step)},
                    input={"context_usage": prepared.usage.model_dump(mode="json")},
                    output={
                        "content": response.content,
                        "reasoning": response.reasoning_content,
                        "refusal": response.refusal,
                    },
                )
            )

            if not response.tool_calls:
                pending_steers = (
                    ()
                    if steering is None
                    else steering.drain_or_transition(
                        resolved_run_id,
                        "validation",
                    )
                )
                if pending_steers:
                    if handle_pending_steers(pending_steers, step=step):
                        continue
                    return failed_outcome()
                trace.root.status = "completed"
                return LoopOutcome(
                    state=_tool_run_state(
                        run_id=resolved_run_id,
                        status="completed",
                        conversation=loop_conversation,
                        trace=trace,
                        context_usage=context_usages,
                        capability_scope=state_scope,
                        workspace_path=(
                            None
                            if execution_context.workspace_path is None
                            else str(execution_context.workspace_path)
                        ),
                    ),
                    execution_context=_format_capability_execution_context(loop_conversation),
                    output_text=response.content.strip(),
                    new_items=tuple(new_items),
                )

            pending_steers = (
                () if steering is None else steering.drain(resolved_run_id)
            )
            if pending_steers:
                if handle_pending_steers(
                    pending_steers,
                    step=step,
                    skipped_calls=response.tool_calls,
                ):
                    continue
                return failed_outcome()

            batch = _PendingToolBatch(
                calls=tuple(self._preflight_call(call, boundary, review_level, capability_ids, workspace)
                            for call in assistant_message.tool_calls),
                boundary=boundary,
            )
            batch_outcome = await process_batch(batch, step=step)
            if batch_outcome is not None:
                return batch_outcome

        if steering is not None:
            steering.discard_and_transition(
                resolved_run_id,
                phase="finishing",
                reason="step_limit_exhausted",
            )
        return failed_outcome()

    async def _compact_history(
        self,
        previous: ContextSummary | None,
        items: tuple[ConversationItem, ...],
        max_tokens: int,
        *,
        run_id: str,
        on_event: LoopEventHandler | None,
        context_assembler: ContextAssembler,
        context_policy: ContextPolicy,
    ) -> ContextSummary:
        if on_event is not None:
            on_event(
                {
                    "type": "context_compaction_started",
                    "run_id": run_id,
                    "item_count": len(items),
                }
            )
        summary_output_limit, source_limit = context_assembler.compaction_limits(
            max_tokens
        )
        source, source_truncated = context_assembler.truncate_text(
            compaction_source(previous, items),
            max_tokens=source_limit,
        )
        system_message = {
            "role": "system",
            "content": (
                "Summarize earlier conversation data for later task continuation. "
                "Preserve facts, decisions, constraints, unresolved work, and important "
                "tool outcomes. Do not include hidden reasoning. Return only the summary."
            ),
        }
        prepared = await context_assembler.prepare(
            system_message=system_message,
            conversation=ConversationState(
                items=(
                    UserMessage(
                        content=source,
                        scope="compactor",
                        visibility="internal",
                    ),
                )
            ),
            policy=context_policy,
            max_output_tokens=summary_output_limit,
            reasoning_effort=context_policy.compaction_reasoning_effort,
            purpose="compaction",
        )
        record_model_turn()
        response = normalize_chat_response(
            chat_response_from_model(await complete_model(self.provider, prepared.request))
        )
        raw_content = response.content.strip()
        if not raw_content:
            raise ValueError("Context compactor returned an empty summary.")
        content, output_truncated = context_assembler.truncate_text(
            raw_content,
            max_tokens=max_tokens,
        )
        summary = ContextSummary(
            content=content,
            source_item_count=(previous.source_item_count if previous else 0) + len(items),
            method="model",
            source_truncated=source_truncated,
            output_truncated=output_truncated,
            reasoning="",
            usage=response.usage,
            model_call=response.metadata,
            context_usage=prepared.usage,
        )
        return summary

    def _llm_tool_definitions(
        self,
        *,
        capability_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.tool_adapter.definitions(
            self.enabled_toolsets,
            capability_ids=capability_ids,
        )

    async def _chat(
        self,
        request: ModelRequest,
        *,
        run_id: str | None,
        step: int,
        on_token: TokenHandler | None,
        on_event: LoopEventHandler | None,
    ) -> ChatResponse:
        async def chat_attempt() -> ChatResponse:
            record_model_turn()
            return chat_response_from_model(await complete_model(self.provider, request))

        if on_token is None and on_event is None:
            return normalize_chat_response(
                await run_with_llm_retries(
                    chat_attempt,
                    policy=self.llm_retry_policy,
                    sleep=self.llm_retry_sleep,
                )
            )

        stream = response_token_stream(
            on_raw=on_token,
            on_event=on_event,
            context=ResponseStreamContext.create(run_id=run_id, model_step=step),
        )
        if stream is None:
            return normalize_chat_response(
                await run_with_llm_retries(
                    chat_attempt,
                    policy=self.llm_retry_policy,
                    sleep=self.llm_retry_sleep,
                )
            )

        response: ChatResponse | None = None
        emitted_tokens = False

        async def attempt() -> ChatResponse:
            nonlocal emitted_tokens, response
            response = None
            record_model_turn()
            if hasattr(self.provider, "stream") or hasattr(self.provider, "stream_chat"):
                async for event in stream_model(self.provider, request):
                    if event.type == "token" and event.content:
                        emitted_tokens = True
                        if getattr(event, "channel", "content") == "reasoning":
                            stream.emit_channel("reasoning", event.content)
                        else:
                            stream(event.content)
                    elif event.type == "done":
                        response = (
                            chat_response_from_model(event.response)
                            if event.response is not None
                            else None
                        )
            else:
                response = chat_response_from_model(
                    await complete_model(self.provider, request)
                )
            return response or ChatResponse()

        try:
            stream.start()
            return normalize_chat_response(
                await run_with_llm_retries(
                    attempt,
                    policy=self.llm_retry_policy,
                    sleep=self.llm_retry_sleep,
                    should_retry=lambda _exc: not emitted_tokens,
                )
            )
        finally:
            stream.finish()

    def _assistant_message(self, response: ChatResponse) -> AssistantMessage:
        return AssistantMessage(
            content=response.content,
            reasoning=response.reasoning_content,
            refusal=response.refusal,
            usage=response.usage,
            model_call=response.metadata,
            tool_calls=tuple(
                ToolCallItem(
                    id=tool_call.id,
                    name=tool_call.name,
                    arguments=tool_call.arguments,
                )
                for tool_call in response.tool_calls
            ),
        )

    def _emit_capability_event(
        self,
        on_event: LoopEventHandler | None,
        invocation: CapabilityInvocation,
        event_type: str,
        *,
        run_id: str,
        content: str | None = None,
    ) -> None:
        if on_event is None:
            return
        payload: dict[str, Any] = {
            "type": event_type,
            "invocation_id": invocation.invocation_id,
            "capability_id": invocation.capability_id,
            "arguments": invocation.arguments,
            "run_id": run_id,
        }
        if content is not None:
            payload["content"] = content
        on_event(payload)


def _format_capability_execution_context(conversation: ConversationState) -> str:
    """Format ToolAgentLoop tool calls for validation and fallback output."""
    lines: list[str] = []
    for message in conversation.items:
        if isinstance(message, ToolResultMessage):
            name = message.name
            content = context_excerpt(
                stored_content_text(message.content),
                limit=MAX_TOOL_RESULT_CONTEXT_CHARS,
            )
            lines.append(f"  - {name}: {content}")

    if lines:
        return context_excerpt(
            "Tool call results:\n" + "\n".join(lines),
            limit=MAX_EXECUTION_CONTEXT_CHARS,
        )

    final_answer = _last_assistant_content(conversation)
    if final_answer:
        return context_excerpt(
            f"Assistant response:\n{final_answer}",
            limit=MAX_EXECUTION_CONTEXT_CHARS,
        )
    return ""


def _append_item(
    conversation: ConversationState,
    item: ConversationItem,
) -> ConversationState:
    return conversation.model_copy(
        update={
            "revision": conversation.revision + 1,
            "items": (*conversation.items, item),
        }
    )


def _tool_result_item(
    *,
    tool_call: ToolCall,
    run_id: str,
    status: str,
    content: str,
    capability_id: str | None = None,
    stored_content: StoredContent | None = None,
    value: Any = None,
    value_reference: ContentReference | None = None,
    artifacts: tuple[Any, ...] = (),
    retention: ResultRetention | None = None,
) -> ToolResultMessage:
    return ToolResultMessage(
        run_id=run_id,
        call_id=tool_call.id,
        name=tool_call.name,
        capability_id=capability_id,
        status=status,  # type: ignore[arg-type]
        content=stored_content or inline_content(content),
        value=value,
        value_reference=value_reference,
        artifacts=artifacts,
        retention=retention,
    )




def _append_skipped_tool_results(
    conversation: ConversationState,
    tool_calls: Sequence[ToolCall],
    *,
    run_id: str,
    content: str,
) -> tuple[ConversationState, list[ToolResultMessage]]:
    skipped: list[ToolResultMessage] = []
    for tool_call in tool_calls:
        item = _tool_result_item(
            tool_call=tool_call,
            run_id=run_id,
            status="skipped",
            content=content,
        )
        conversation = _append_item(conversation, item)
        skipped.append(item)
    return conversation, skipped


def _tool_run_state(
    *,
    run_id: str,
    status: str,
    conversation: ConversationState,
    trace: RunTrace,
    context_usage: list[ContextUsage],
    pending_review: PendingReview | None = None,
    pending_tool_batch: _PendingToolBatch | None = None,
    capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
    workspace_path: str | None = None,
) -> RunState:
    return RunState(
        run_id=run_id,
        kind="tool",
        status=status,  # type: ignore[arg-type]
        conversation=conversation,
        model_thread=conversation,
        context_usage=context_usage,
        trace=trace,
        pending_review=pending_review,
        pending_tool_batch=pending_tool_batch,
        capability_scope=capability_scope_to_state(capability_scope),
        runtime_mode="tool",
        workspace_path=workspace_path,
    )


def _state_capability_scope(
    capability_scope: CapabilityScope,
    *,
    capability_ids: Sequence[str] | None,
    skills: tuple[str, ...] | None,
) -> CapabilityScope:
    if capability_scope != DEFAULT_CAPABILITY_SCOPE:
        return capability_scope
    if capability_ids is None and skills is None:
        return capability_scope
    return CapabilityScope(
        capability_ids=None if capability_ids is None else tuple(capability_ids),
        skills=skills,
    )


def _tool_content(result: CapabilityResult) -> str:
    if result.status == "completed":
        return result.content
    prefix = "[BOUNDARY_VIOLATION]" if result.stop_reason == "BoundaryViolation" else "[TOOL_ERROR]"
    return f"{prefix} {result.error or result.content}"


def _boundary_with_reviewed_paths(boundary: Boundary, payload: dict[str, Any]) -> Boundary:
    raw_paths = payload.get("boundary_paths")
    if not isinstance(raw_paths, list):
        return boundary
    allowed_paths = list(boundary.allowed_paths)
    for path in raw_paths:
        if isinstance(path, str) and path and path not in allowed_paths:
            allowed_paths.append(path)
    return boundary.model_copy(update={"allowed_paths": allowed_paths})


def _reviewable_boundary_result(result: CapabilityResult) -> bool:
    return result.status == "failed" and result.stop_reason == "BoundaryViolation"


def _exception_message(exc: Exception) -> str:
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


def _find_capability_node(node: RunTraceNode, invocation_id: str) -> RunTraceNode | None:
    if node.kind == "capability_call" and node.ref.get("invocation_id") == invocation_id:
        return node
    for child in node.children:
        found = _find_capability_node(child, invocation_id)
        if found is not None:
            return found
    return None


def _execution_context(
    context: CapabilityExecutionContext | None,
    *,
    task_id: str,
    skills: tuple[str, ...] | None,
) -> CapabilityExecutionContext:
    base = context or CapabilityExecutionContext(task_id=task_id)
    return replace(base, skills=skills)


def _last_assistant_content(conversation: ConversationState) -> str:
    for message in reversed(conversation.items):
        if isinstance(message, AssistantMessage) and message.content:
            return message.content
    return ""
