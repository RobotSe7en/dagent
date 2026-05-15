"""Top-level harness runtime.

The runtime orchestrates: route -> loop -> review -> validation -> final result.
It does not know how loops execute internally; it only consumes LoopOutcome.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from dagent.harness_runtime.tool_agent import (
    ToolAgent,
    LoopEventHandler,
    TokenHandler,
)
from dagent.harness_runtime.dag_agent import DAGAgent
from dagent.harness_runtime.validator_agent import ValidatorAgent, format_validation_feedback
from dagent.harness_runtime.review_policy import ReviewLevel
from dagent.harness_runtime.runtime_session import HarnessRuntimeSession
from dagent.harness_runtime.runtime_events import (
    _ThinkTagFilter,
    _dag_event_emitter,
    _trace_event_emitter,
)
from dagent.providers import ChatProvider
from dagent.runnables import RunnableExecutor, RunnableRegistry
from dagent.runnables.providers import FileRunnableProvider, MemoryRunnableProvider, ToolRunnableProvider
from dagent.schemas import DAG, LoopOutcome, RuntimeResponse, RunnableInvocation


RuntimeMode = Literal["auto", "tool", "dag"]

_ROUTE_SYSTEM_PROMPT = """\
You are a routing classifier. Given the user message, \
decide whether the request needs a multi-step DAG pipeline or can be handled by a \
single bounded tool-agent loop.

Respond with exactly one word: "dag" or "tool".

Choose "dag" ONLY when the task clearly benefits from node-level planning, \
parallelism, human review gates, resumability, or multi-agent coordination.

Choose "tool" for greetings, simple Q&A, single tool calls, ordinary short \
serial work, or anything that does not need structured orchestration.\
"""


LoopRunner = Callable[[str | None], Awaitable[LoopOutcome]]


class HarnessRuntime:
    """Orchestrates: route -> loop -> review -> validation -> final result."""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        tool_agent: ToolAgent,
        dag_agent: DAGAgent,
        validator: ValidatorAgent | None = None,
        enable_validation: bool = False,
        max_validation_retries: int = 1,
        runnable_registry: RunnableRegistry | None = None,
        runnable_executor: RunnableExecutor | None = None,
    ) -> None:
        self.provider = provider
        self.tool_agent = tool_agent
        self.dag_agent = dag_agent
        self.validator = validator
        self.enable_validation = enable_validation
        self.max_validation_retries = max_validation_retries
        self.session = HarnessRuntimeSession()
        self.tasks = self.session.tasks
        self.runnable_registry = runnable_registry or RunnableRegistry()
        self.runnable_executor = runnable_executor or RunnableExecutor(self.runnable_registry)
        self._register_default_runnables()

    def _register_default_runnables(self) -> None:
        if self.runnable_registry.ids():
            return
        tool_executor = getattr(getattr(self.tool_agent, "loop", None), "tool_executor", None)
        if tool_executor is not None:
            ToolRunnableProvider(tool_executor.registry).register_into(
                self.runnable_registry,
                self.runnable_executor,
            )
        MemoryRunnableProvider().register_into(self.runnable_registry, self.runnable_executor)
        FileRunnableProvider().register_into(self.runnable_registry, self.runnable_executor)

    # ==================================================================
    # Public API
    # ==================================================================

    async def _validate_loop_outcome(
        self,
        loop_outcome: LoopOutcome,
        user_request: str,
        *,
        on_event: LoopEventHandler | None,
    ) -> tuple[bool, str | None]:
        passed = True
        feedback: str | None = None
        if not self.enable_validation or not self.validator:
            return passed, feedback
        if not loop_outcome.execution_context.strip():
            return passed, feedback
        if on_event:
            on_event({"type": "validating", "message": "Validating result quality..."})
        validation = await self.validator.validate(
            user_request=user_request,
            final_answer=loop_outcome.final_answer,
            execution_context=loop_outcome.execution_context,
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

    async def _run_with_review_and_validation(
        self,
        user_request: str,
        *,
        run_once: LoopRunner,
        initial_loop_outcome: LoopOutcome | None = None,
        on_event: LoopEventHandler | None,
    ) -> LoopOutcome:
        feedback: str | None = None
        loop_outcome: LoopOutcome | None = initial_loop_outcome

        for _attempt in range(self.max_validation_retries + 1):
            if loop_outcome is None or feedback is not None:
                loop_outcome = await run_once(feedback)

            if loop_outcome.status == "awaiting_review":
                return loop_outcome

            passed, validation_feedback = await self._validate_loop_outcome(
                loop_outcome,
                user_request,
                on_event=on_event,
            )
            if passed:
                return loop_outcome
            feedback = validation_feedback

        if loop_outcome is None:
            raise RuntimeError("Validation retry loop did not execute.")
        return loop_outcome

    async def handle_message(
        self,
        message: str,
        *,
        mode: RuntimeMode = "auto",
        review_level: ReviewLevel = "fast",
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RuntimeResponse:
        # 1. Route
        resolved_mode = mode
        if mode == "auto":
            resolved_mode = await self._route(message)

        async def run_once(feedback: str | None) -> LoopOutcome:
            return await self._execute_loop(
                feedback or message,
                mode=resolved_mode,
                review_level=review_level,
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
            runtime_mode=resolved_mode,
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
    ) -> RuntimeResponse | None:
        state = self.session.pop_review_continuation(review_id)
        if state is None:
            return None
        if state.kind == "tool_review":
            thinking_only = _ThinkTagFilter(on_token, keep="inside") if on_token else None
            initial_outcome = await self.tool_agent.resume_review(
                state,
                approved=approved,
                on_token=thinking_only,
                on_event=on_event,
            )
            if initial_outcome is None:
                return None

            async def run_once(feedback: str | None) -> LoopOutcome:
                if feedback is None:
                    raise RuntimeError("Tool review validation retry requires feedback.")
                return await self.tool_agent.run(
                    feedback,
                    review_level=state.review_level,
                    on_token=thinking_only,
                    on_event=on_event,
                )

            outcome = await self._run_with_review_and_validation(
                state.user_request,
                run_once=run_once,
                initial_loop_outcome=initial_outcome,
                on_event=on_event,
            )
            return self._finish_loop_outcome(
                outcome,
                state.user_request,
                "tool",
                state.review_level,
                extra_invocations=state.invocations,
                task_id=state.task_id,
            )

        if approved and dag is None:
            return None
        task_id = state.task_id
        record = self.tasks[task_id]
        self.session.discard_review_continuations_for_task(task_id)
        thinking_only = _ThinkTagFilter(on_token, keep="inside") if on_token else None
        initial_outcome = await self.dag_agent.resume_review(
            state,
            record=record,
            dag=dag,
            approved=approved,
            review_level=review_level,
            on_token=thinking_only,
            on_trace=_trace_event_emitter(on_event),
            on_dag=_dag_event_emitter(on_event),
        )
        if initial_outcome is None:
            return None
        if initial_outcome.status == "awaiting_review":
            return self._finish_loop_outcome(
                initial_outcome,
                record.user_request,
                "dag",
                review_level or state.review_level,
                task_id=task_id,
                runtime_mode=record.runtime_mode,
            )

        async def run_once(feedback: str | None) -> LoopOutcome:
            if feedback is None:
                raise RuntimeError("DAG review validation retry requires feedback.")
            return await self.dag_agent.run(
                feedback,
                task_id=record.task_id,
                review_level=review_level or state.review_level,
                runtime_mode=record.runtime_mode,
                on_token=thinking_only,
                on_trace=_trace_event_emitter(on_event),
                on_dag=_dag_event_emitter(on_event),
            )

        outcome = await self._run_with_review_and_validation(
            record.user_request,
            run_once=run_once,
            initial_loop_outcome=initial_outcome,
            on_event=on_event,
        )
        return self._finish_loop_outcome(
            outcome,
            record.user_request,
            "dag",
            review_level or state.review_level,
            task_id=task_id,
            runtime_mode=record.runtime_mode,
        )

    # ==================================================================
    # Route
    # ==================================================================

    async def _route(self, message: str) -> Literal["dag", "tool"]:
        """Lightweight LLM classifier."""
        messages = [
            {"role": "system", "content": _ROUTE_SYSTEM_PROMPT},
            {"role": "user", "content": message},
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
        on_token: TokenHandler | None,
        on_event: LoopEventHandler | None,
    ) -> LoopOutcome:
        """Dispatch to the appropriate loop and return a unified LoopOutcome."""
        if mode == "dag":
            thinking_only = _ThinkTagFilter(on_token, keep="inside") if on_token else None
            return await self.dag_agent.run(
                message,
                task_id=None,
                review_level=review_level,
                runtime_mode=str(mode),
                on_token=thinking_only,
                on_trace=_trace_event_emitter(on_event),
                on_dag=_dag_event_emitter(on_event),
            )
        elif mode == "tool":
            # Tool mode: only stream <think> blocks from the loop.
            # The final answer is returned in the done payload.
            thinking_only = _ThinkTagFilter(on_token, keep="inside") if on_token else None
            return await self.tool_agent.run(
                message,
                review_level=review_level,
                on_token=thinking_only,
                on_event=on_event,
            )
        else:
            raise ValueError(f"Unknown runtime mode: {mode}")

    def _finish_loop_outcome(
        self,
        outcome: LoopOutcome,
        user_request: str,
        mode: Literal["tool", "dag"],
        review_level: ReviewLevel,
        *,
        extra_invocations: list[RunnableInvocation] | None = None,
        task_id: str | None = None,
        runtime_mode: str | None = None,
    ) -> RuntimeResponse:
        invocations = [*(extra_invocations or []), *outcome.invocations]
        if outcome.status == "awaiting_review":
            record = self.session.record_outcome(
                task_id=task_id,
                mode=mode,
                user_request=user_request,
                review_level=review_level,
                loop_outcome=outcome,
                invocations=invocations,
                runtime_mode=runtime_mode,
            )
            return _gate_result_for_task(outcome, record.task_id)

        record = self.session.record_outcome(
            task_id=task_id,
            mode=mode,
            user_request=user_request,
            review_level=review_level,
            loop_outcome=outcome,
            invocations=invocations,
            runtime_mode=runtime_mode,
        )
        final_answer = outcome.final_answer.strip() or _fallback_final_answer(outcome)
        return RuntimeResponse(
            status=outcome.status,
            final_answer=final_answer,
            dag=outcome.dag,
            dag_run=outcome.dag_run,
            task_id=record.task_id,
            events=outcome.events,
            pending_review=outcome.pending_review,
        )


def _fallback_final_answer(loop_outcome: LoopOutcome) -> str:
    if loop_outcome.execution_context.strip():
        return loop_outcome.execution_context.strip()
    if loop_outcome.status == "completed":
        return "The task completed, but no final answer was produced."
    return "The task did not complete, and no final answer was produced."


def _gate_result_for_task(loop_outcome: LoopOutcome, task_id: str) -> RuntimeResponse:
    return RuntimeResponse(
        status="awaiting_review",
        final_answer="",
        dag=loop_outcome.dag,
        dag_run=loop_outcome.dag_run,
        task_id=task_id,
        events=loop_outcome.events,
        pending_review=loop_outcome.pending_review,
    )

