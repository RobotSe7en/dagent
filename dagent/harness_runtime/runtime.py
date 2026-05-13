"""Top-level harness runtime.

The runtime orchestrates: route -> loop -> review -> validation -> final result.
It does not know how loops execute internally; it only consumes LoopResult.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from dagent.harness_runtime.tool_agent import (
    ToolAgentLoop,
    LoopEventHandler,
    TokenHandler,
)
from dagent.harness_runtime.dag_executor import RunResult
from dagent.harness_runtime.dag_agent import DAGAgentLoop
from dagent.harness_runtime.loop_result import LoopResult
from dagent.harness_runtime.result_validator import ResultValidatorAgent, format_validation_feedback
from dagent.harness_runtime.review_policy import ReviewLevel
from dagent.harness_runtime.runtime_session import HarnessRuntimeSession
from dagent.harness_runtime.runtime_events import (
    _ThinkTagFilter,
    _dag_event_emitter,
    _trace_event_emitter,
)
from dagent.harness_runtime.task_record import ReviewContinuation, PendingReview
from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider
from dagent.schemas import Boundary, DAG, ToolInvocation
from dagent.state import PromptBuilder, PromptRequest
from dagent.tools.registry import Tool


RuntimeMode = Literal["auto", "tool", "dag"]

_ROUTE_SYSTEM_PROMPT = """\
You are a routing classifier.  Given the user message and conversation context, \
decide whether the request needs a multi-step DAG pipeline or can be handled by a \
single bounded tool-agent loop.

Respond with exactly one word: "dag" or "tool".

Choose "dag" ONLY when the task clearly benefits from node-level planning, \
parallelism, human review gates, resumability, or multi-agent coordination.

Choose "tool" for greetings, simple Q&A, single tool calls, ordinary short \
serial work, or anything that does not need structured orchestration.\
"""


@dataclass(frozen=True)
class HarnessMessageResult:
    status: Literal["completed", "awaiting_review", "failed"]
    final_answer: str
    dag: DAG | None = None
    run_result: RunResult | None = None
    task_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    pending_review: PendingReview | None = None


@dataclass(frozen=True)
class _LoopFlowOutcome:
    loop_result: LoopResult
    needs_human_review: bool = False


LoopRunner = Callable[[str | None, LoopResult | None], Awaitable[LoopResult]]
class HarnessRuntime:
    """Orchestrates: route -> loop -> review -> validation -> final result."""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        tool_agent_loop: ToolAgentLoop,
        dag_agent_loop: DAGAgentLoop,
        conversation_profile: AgentProfile,
        runtime_tools: list[Tool] | None = None,
        prompt_builder: PromptBuilder | None = None,
        validator: ResultValidatorAgent | None = None,
        enable_validation: bool = False,
        max_top_steps: int = 8,
        max_validation_retries: int = 1,
    ) -> None:
        self.provider = provider
        self.tool_agent_loop = tool_agent_loop
        self.dag_agent_loop = dag_agent_loop
        self.conversation_profile = conversation_profile
        self.runtime_tools = runtime_tools or []
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.validator = validator
        self.enable_validation = enable_validation
        self.max_top_steps = max_top_steps
        self.max_validation_retries = max_validation_retries
        self.session = HarnessRuntimeSession(initial_tasks=dag_agent_loop.tasks)
        self.tasks = self.session.tasks
        self.dag_agent_loop.tasks = self.tasks

    # ==================================================================
    # Public API
    # ==================================================================

    async def _validate_loop_result(
        self,
        loop_result: LoopResult,
        user_request: str,
        *,
        on_event: LoopEventHandler | None,
    ) -> tuple[bool, str | None]:
        passed = True
        feedback: str | None = None
        if not self.enable_validation or not self.validator:
            return passed, feedback
        if not loop_result.execution_context.strip():
            return passed, feedback
        if on_event:
            on_event({"type": "validating", "message": "Validating result quality..."})
        validation = await self.validator.validate(
            user_request=user_request,
            final_answer=loop_result.final_answer,
            execution_context=loop_result.execution_context,
        )
        if validation.passed:
            if on_event:
                on_event({
                    "type": "validation_passed",
                    "passed": True,
                    "summary": validation.summary,
                    "issues": [
                        {"message": issue.message, "node_id": issue.node_id}
                        for issue in validation.issues
                    ],
                })
            return passed, feedback
        feedback = format_validation_feedback(validation)
        passed = False
        if on_event:
            on_event({
                "type": "retry",
                "reason": feedback,
                "summary": validation.summary,
                "issues": [
                    {"message": issue.message, "node_id": issue.node_id}
                    for issue in validation.issues
                ],
            })
        return passed, feedback

    def _build_final_result(
        self,
        loop_result: LoopResult,
        user_request: str,
        *,
        task_id: str,
    ) -> HarnessMessageResult:
        final_answer = loop_result.final_answer.strip() or _fallback_final_answer(loop_result)
        if not loop_result.messages:
            self.session.append_conversation_turn(user_request, final_answer)
        return HarnessMessageResult(
            status="completed" if loop_result.completed else "failed",
            final_answer=final_answer,
            dag=loop_result.dag,
            run_result=loop_result.run_result,
            task_id=task_id,
            events=loop_result.events,
            pending_review=loop_result.pending_review,
        )

    async def _run_with_review_and_validation(
        self,
        user_request: str,
        *,
        run_once: LoopRunner,
        initial_loop_result: LoopResult | None = None,
        on_event: LoopEventHandler | None,
    ) -> _LoopFlowOutcome:
        feedback: str | None = None
        loop_result: LoopResult | None = initial_loop_result

        for _attempt in range(self.max_validation_retries + 1):
            if loop_result is None or feedback is not None:
                loop_result = await run_once(feedback, loop_result)

            if loop_result.needs_human_review:
                return _LoopFlowOutcome(
                    loop_result=loop_result,
                    needs_human_review=True,
                )

            passed, validation_feedback = await self._validate_loop_result(
                loop_result,
                user_request,
                on_event=on_event,
            )
            if passed:
                return _LoopFlowOutcome(loop_result=loop_result)
            feedback = validation_feedback

        if loop_result is None:
            raise RuntimeError("Validation retry loop did not execute.")
        return _LoopFlowOutcome(loop_result=loop_result)

    async def handle_message(
        self,
        message: str,
        *,
        mode: RuntimeMode = "auto",
        review_level: ReviewLevel = "fast",
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult:
        # 1. Route
        resolved_mode = mode
        if mode == "auto":
            resolved_mode = await self._route(message)

        async def run_once(feedback: str | None, previous_result: LoopResult | None) -> LoopResult:
            prior_messages = previous_result.messages if feedback and previous_result else None
            return await self._execute_loop(
                message,
                mode=resolved_mode,
                review_level=review_level,
                feedback=feedback,
                prior_messages=prior_messages,
                on_token=on_token,
                on_event=on_event,
            )

        outcome = await self._run_with_review_and_validation(
            message,
            run_once=run_once,
            on_event=on_event,
        )
        return self._finish_loop_outcome(
            outcome,
            message,
            resolved_mode,
            review_level,
            record_conversation=True,
        )

    async def resume_review(
        self,
        review_id: str,
        *,
        dag: DAG | None = None,
        approved: bool = True,
        review_level: ReviewLevel | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult | None:
        state = self.session.pop_review_continuation(review_id)
        if state is None:
            return None
        if state.kind == "tool_review":
            return await self._resume_tool_review(
                state,
                approved=approved,
                on_token=on_token,
                on_event=on_event,
            )
        if dag is None:
            return None
        return await self._resume_dag_review(
            state,
            dag,
            review_level=review_level,
            on_token=on_token,
            on_event=on_event,
        )

    async def _resume_dag_review(
        self,
        state: ReviewContinuation,
        dag: DAG,
        review_level: ReviewLevel | None = None,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult:
        task_id = state.task_id
        record = self.tasks[task_id]
        self.session.discard_review_continuations_for_task(task_id)
        thinking_only = _ThinkTagFilter(on_token, keep="inside") if on_token else None

        dag_result = await self.dag_agent_loop.resume(
            task_id,
            dag,
            review_level=review_level,
            on_token=thinking_only,
            on_trace=_trace_event_emitter(on_event),
            on_dag=_dag_event_emitter(on_event),
        )
        loop_result = dag_result.to_loop_result()

        if loop_result.needs_human_review:
            return self._finish_loop_outcome(
                _LoopFlowOutcome(
                    loop_result=loop_result,
                    needs_human_review=True,
                ),
                record.user_request,
                "dag",
                review_level or state.review_level,
                task_id=task_id,
            )

        async def run_once(feedback: str | None, previous_result: LoopResult | None) -> LoopResult:
            planning_context = self.session.tasks_context()
            if feedback:
                planning_context = (
                    f"{planning_context}\n\n{feedback}" if planning_context else feedback
                )
            retry_dag_result = await self.dag_agent_loop.run(
                record.user_request,
                review_level=review_level or state.review_level,
                planning_context=planning_context,
                runtime_mode=record.runtime_mode,
                conversation_history=self.session.conversation_history,
                on_token=thinking_only,
                on_trace=_trace_event_emitter(on_event),
                on_dag=_dag_event_emitter(on_event),
            )
            return retry_dag_result.to_loop_result()

        outcome = await self._run_with_review_and_validation(
            record.user_request,
            run_once=run_once,
            initial_loop_result=loop_result,
            on_event=on_event,
        )
        return self._finish_loop_outcome(
            outcome,
            record.user_request,
            "dag",
            review_level or state.review_level,
            task_id=task_id,
        )

    # ==================================================================
    # Route
    # ==================================================================

    async def _route(self, message: str) -> Literal["dag", "tool"]:
        """Lightweight LLM classifier."""
        context = self.session.tasks_context()
        user_content = message
        if context:
            user_content = f"Conversation context:\n{context}\n\nUser message:\n{message}"

        messages = [
            {"role": "system", "content": _ROUTE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        try:
            response = await self.provider.chat(messages)
            decision = response.content.strip().lower()
            if decision in {"dag", "tool"}:
                return decision  # type: ignore[return-value]
        except Exception:
            pass
        return "tool"

    # ==================================================================
    # Loop execution
    # ==================================================================

    async def _execute_loop(
        self,
        message: str,
        *,
        mode: RuntimeMode,
        review_level: ReviewLevel,
        feedback: str | None,
        prior_messages: list[dict[str, Any]] | None = None,
        on_token: TokenHandler | None,
        on_event: LoopEventHandler | None,
    ) -> LoopResult:
        """Dispatch to the appropriate loop and return a unified LoopResult."""
        if mode == "dag":
            planning_context = self.session.tasks_context()
            if feedback:
                planning_context = (planning_context + "\n\n" + feedback) if planning_context else feedback
            thinking_only = _ThinkTagFilter(on_token, keep="inside") if on_token else None
            dag_result = await self.dag_agent_loop.run(
                message,
                review_level=review_level,
                planning_context=planning_context,
                runtime_mode=str(mode),
                conversation_history=self.session.conversation_history,
                on_token=thinking_only,
                on_trace=_trace_event_emitter(on_event),
                on_dag=_dag_event_emitter(on_event),
            )
            return dag_result.to_loop_result()
        elif mode == "tool":
            # Tool mode: only stream <think> blocks from the loop.
            # The final answer is returned in the done payload.
            thinking_only = _ThinkTagFilter(on_token, keep="inside") if on_token else None
            return await self._run_tool(
                message,
                review_level=review_level,
                feedback=feedback,
                prior_messages=prior_messages,
                on_token=thinking_only,
                on_event=on_event,
            )
        else:
            raise ValueError(f"Unknown runtime mode: {mode}")

    async def _run_tool(
        self,
        message: str,
        *,
        review_level: ReviewLevel = "fast",
        feedback: str | None = None,
        prior_messages: list[dict[str, Any]] | None = None,
        on_token: TokenHandler | None,
        on_event: LoopEventHandler | None,
    ) -> LoopResult:
        """Run ToolAgentLoop and return unified LoopResult."""
        if prior_messages and feedback:
            messages = list(prior_messages)
            messages.append({"role": "user", "content": feedback})
        else:
            base_messages = self.prompt_builder.build(
                PromptRequest(
                    profile=self.conversation_profile,
                    task_content="{{ user_message }}",
                    tools=self.runtime_tools,
                    memory=self.conversation_profile.memory,
                    context=self.session.tasks_context(),
                    variables={"user_message": message},
                )
            )
            system_msg = base_messages[0]
            current_user_msg = base_messages[1]
            messages = [system_msg, *self.session.conversation_history, current_user_msg]

        boundary = Boundary(mode="read_only", allowed_paths=["."])
        control_tool_names = self._reviewable_tool_names()
        result = await self.tool_agent_loop.run(
            "",
            boundary=boundary,
            max_steps=self.max_top_steps,
            messages=messages,
            control_tool_names=control_tool_names,
            control_tool_handler=self.tool_agent_loop.create_tool_guard(review_level, boundary, self.runtime_tools),
            on_token=on_token,
            on_event=on_event,
        )
        return result.to_loop_result()

    def _reviewable_tool_names(self) -> set[str]:
        return {
            tool.name
            for tool in self.runtime_tools
            if tool.risk in {"medium", "high"} or tool.risk_fn is not None
        }

    async def _continue_tool_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        state: ReviewContinuation,
        on_token: TokenHandler | None,
        on_event: LoopEventHandler | None,
    ) -> LoopResult:
        thinking_only = _ThinkTagFilter(on_token, keep="inside") if on_token else None
        result = await self.tool_agent_loop.run(
            "",
            boundary=state.boundary,
            max_steps=self.max_top_steps,
            messages=messages,
            control_tool_names=self._reviewable_tool_names(),
            control_tool_handler=self.tool_agent_loop.create_tool_guard(
                state.review_level,
                state.boundary,
                self.runtime_tools,
            ),
            on_token=thinking_only,
            on_event=on_event,
        )
        return result.to_loop_result()

    async def _resume_tool_review(
        self,
        state: ReviewContinuation,
        *,
        approved: bool,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult | None:
        if (
            state.kind != "tool_review"
            or state.tool_call_id is None
            or state.tool_name is None
            or state.boundary is None
        ):
            return None

        feed_content = "[DENIED] Tool '{name}' was rejected by the user. Do not retry this exact tool call.".format(name=state.tool_name)
        if approved:
            try:
                feed_content = self.tool_agent_loop.tool_executor.execute(
                    state.tool_name, state.tool_args, boundary=state.boundary
                )
            except Exception as exc:
                feed_content = f"[TOOL_ERROR] {type(exc).__name__}: {exc}"

        state.messages.append({
            "role": "tool",
            "tool_call_id": state.tool_call_id,
            "name": state.tool_name,
            "content": feed_content,
        })

        async def run_once(feedback: str | None, previous_result: LoopResult | None) -> LoopResult:
            messages = state.messages
            if feedback:
                prior_messages = previous_result.messages if previous_result else messages
                messages = [*prior_messages, {"role": "user", "content": feedback}]
            return await self._continue_tool_messages(
                messages,
                state=state,
                on_token=on_token,
                on_event=on_event,
            )

        outcome = await self._run_with_review_and_validation(
            state.user_request,
            run_once=run_once,
            on_event=on_event,
        )
        return self._finish_loop_outcome(
            outcome,
            state.user_request,
            "tool",
            state.review_level,
            record_conversation=True,
            extra_invocations=state.invocations,
            task_id=state.task_id,
        )

    def _finish_loop_outcome(
        self,
        outcome: _LoopFlowOutcome,
        user_request: str,
        mode: Literal["tool", "dag"],
        review_level: ReviewLevel,
        *,
        record_conversation: bool = False,
        extra_invocations: list[ToolInvocation] | None = None,
        task_id: str | None = None,
    ) -> HarnessMessageResult:
        if outcome.needs_human_review:
            record = self.session.record_loop_outcome(
                task_id=task_id,
                mode=mode,
                user_request=user_request,
                review_level=review_level,
                needs_human_review=True,
                loop_result=outcome.loop_result,
                invocations=[*(extra_invocations or []), *outcome.loop_result.invocations],
                review_boundary=_review_boundary(outcome.loop_result),
            )
            return _gate_result_for_task(outcome.loop_result, record.task_id)

        if record_conversation:
            self.session.record_conversation(outcome.loop_result)
        record = self.session.record_loop_outcome(
            task_id=task_id,
            mode=mode,
            user_request=user_request,
            review_level=review_level,
            needs_human_review=False,
            loop_result=outcome.loop_result,
            invocations=[*(extra_invocations or []), *outcome.loop_result.invocations],
        )
        return self._build_final_result(outcome.loop_result, user_request, task_id=record.task_id)


def _fallback_final_answer(loop_result: LoopResult) -> str:
    if loop_result.execution_context.strip():
        return loop_result.execution_context.strip()
    if loop_result.completed:
        return "The task completed, but no final answer was produced."
    return "The task did not complete, and no final answer was produced."


def _gate_result_for_task(loop_result: LoopResult, task_id: str) -> HarnessMessageResult:
    return HarnessMessageResult(
        status="awaiting_review",
        final_answer="",
        dag=loop_result.dag,
        run_result=loop_result.run_result,
        task_id=task_id,
        events=loop_result.events,
        pending_review=loop_result.pending_review,
    )


def _review_boundary(loop_result: LoopResult) -> Boundary | None:
    review = loop_result.pending_review
    if review is None or review.tool_call is None:
        return None
    tool_call_id = review.tool_call.get("tool_call_id")
    for invocation in reversed(loop_result.invocations):
        if invocation.invocation_id == tool_call_id:
            return invocation.boundary
    if loop_result.invocations:
        return loop_result.invocations[-1].boundary
    return None
