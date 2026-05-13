"""Top-level harness runtime.

The runtime orchestrates: route -> loop -> review -> summarize.
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
from dagent.harness_runtime.task_record import ToolTaskState, PendingReview
from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider
from dagent.schemas import Boundary, DAG
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
    status: Literal["completed", "awaiting_dag_review", "awaiting_tool_review", "awaiting_change_review", "awaiting_approval", "failed"]
    message_markdown: str
    dag: DAG | None = None
    run_result: RunResult | None = None
    task_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    pending_review: PendingReview | None = None


@dataclass(frozen=True)
class _GateFlowOutcome:
    loop_result: LoopResult
    gate: HarnessMessageResult | None = None


LoopRunner = Callable[[str | None, LoopResult | None], Awaitable[LoopResult]]
ReviewGateCallback = Callable[[LoopResult], None]


class HarnessRuntime:
    """Orchestrates: route -> loop -> review -> summarize."""

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
        self.tasks = dag_agent_loop.tasks
        self.session = HarnessRuntimeSession(tasks=self.tasks)

    # ==================================================================
    # Public API
    # ==================================================================

    def _review_status(self, pending_review: PendingReview | None) -> str:
        if pending_review and pending_review.kind == "tool_review":
            return "awaiting_tool_review"
        if pending_review and pending_review.kind == "initial_dag":
            return "awaiting_dag_review"
        return "awaiting_change_review"

    def _human_review_gate(self, loop_result: LoopResult) -> HarnessMessageResult | None:
        if not loop_result.needs_human_review:
            return None
        return HarnessMessageResult(
            status=self._review_status(loop_result.pending_review),
            message_markdown="",
            dag=loop_result.dag,
            run_result=loop_result.run_result,
            task_id=loop_result.task_id,
            events=loop_result.events,
            pending_review=loop_result.pending_review,
        )

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

    async def _build_summary_result(
        self,
        loop_result: LoopResult,
        user_request: str,
        *,
        on_token: TokenHandler | None,
    ) -> HarnessMessageResult:
        summary = await self._summarize(
            user_request=user_request,
            execution_context=loop_result.execution_context,
            final_answer=loop_result.final_answer,
            on_token=on_token,
        )
        if not loop_result.messages:
            self.session.append_conversation_turn(user_request, summary)
        return HarnessMessageResult(
            status="completed" if loop_result.completed else "failed",
            message_markdown=summary,
            dag=loop_result.dag,
            run_result=loop_result.run_result,
            task_id=loop_result.task_id,
            events=loop_result.events,
            pending_review=loop_result.pending_review,
        )

    async def _run_with_review_and_validation(
        self,
        user_request: str,
        *,
        run_once: LoopRunner,
        initial_loop_result: LoopResult | None = None,
        on_gate: ReviewGateCallback | None = None,
        on_event: LoopEventHandler | None,
    ) -> _GateFlowOutcome:
        feedback: str | None = None
        loop_result: LoopResult | None = initial_loop_result

        for _attempt in range(self.max_validation_retries + 1):
            if loop_result is None or feedback is not None:
                loop_result = await run_once(feedback, loop_result)

            gate = self._human_review_gate(loop_result)
            if gate is not None:
                if on_gate is not None:
                    on_gate(loop_result)
                return _GateFlowOutcome(loop_result=loop_result, gate=gate)

            passed, validation_feedback = await self._validate_loop_result(
                loop_result,
                user_request,
                on_event=on_event,
            )
            if passed:
                return _GateFlowOutcome(loop_result=loop_result)
            feedback = validation_feedback

        if loop_result is None:
            raise RuntimeError("Validation retry loop did not execute.")
        return _GateFlowOutcome(loop_result=loop_result)

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

        def on_gate(loop_result: LoopResult) -> None:
            if loop_result.pending_review and loop_result.pending_review.kind == "tool_review":
                self._store_tool_review_state(message, review_level, loop_result)

        outcome = await self._run_with_review_and_validation(
            message,
            run_once=run_once,
            on_gate=on_gate,
            on_event=on_event,
        )
        if outcome.gate is not None:
            return outcome.gate

        self.session.record_conversation(outcome.loop_result)
        return await self._build_summary_result(outcome.loop_result, message, on_token=on_token)

    async def resume_dag(
        self,
        task_id: str,
        dag: DAG,
        review_level: ReviewLevel | None = None,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult:
        record = self.tasks[task_id]
        was_completed = record.dag.status == "completed" and len(record.runs) > 0
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

        gate = self._human_review_gate(loop_result)
        if gate is not None:
            return gate

        if was_completed and dag_result.run_result is record.runs[-1]:
            return HarnessMessageResult(
                status="completed",
                message_markdown="",
                dag=loop_result.dag,
                run_result=loop_result.run_result,
                task_id=task_id,
                events=loop_result.events,
            )

        async def run_once(feedback: str | None, previous_result: LoopResult | None) -> LoopResult:
            planning_context = self.session.tasks_context()
            if feedback:
                planning_context = (
                    f"{planning_context}\n\n{feedback}" if planning_context else feedback
                )
            retry_dag_result = await self.dag_agent_loop.run(
                record.user_request,
                review_level=review_level or record.review_level,
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
        if outcome.gate is not None:
            return outcome.gate

        return await self._build_summary_result(outcome.loop_result, record.user_request, on_token=on_token)

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
            # The actual answer comes from _summarize().
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

    def _store_tool_review_state(
        self,
        user_request: str,
        review_level: ReviewLevel,
        loop_result: LoopResult,
    ) -> None:
        self.session.store_tool_review_state(
            user_request=user_request,
            review_level=review_level,
            loop_result=loop_result,
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
        )

    async def _continue_tool_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        state: ToolTaskState,
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

    async def resume_tool(
        self,
        review_id: str,
        approved: bool,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult | None:
        state = self.session.pop_tool_review_state(review_id)
        if state is None:
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

        def on_gate(loop_result: LoopResult) -> None:
            if loop_result.pending_review and loop_result.pending_review.kind == "tool_review":
                self._store_tool_review_state(state.user_request, state.review_level, loop_result)

        outcome = await self._run_with_review_and_validation(
            state.user_request,
            run_once=run_once,
            on_gate=on_gate,
            on_event=on_event,
        )
        if outcome.gate is not None:
            return outcome.gate

        self.session.record_conversation(outcome.loop_result)
        return await self._build_summary_result(outcome.loop_result, state.user_request, on_token=on_token)

    # ==================================================================
    # Summarize
    # ==================================================================

    async def _summarize(
        self,
        *,
        user_request: str,
        execution_context: str,
        final_answer: str = "",
        on_token: TokenHandler | None = None,
    ) -> str:
        """Streaming LLM call that produces the user-facing answer."""
        task_content = (
            "You are summarizing the results of a task execution for the user.\n\n"
            "User's original request:\n{{ user_request }}\n\n"
            "Execution results:\n{{ execution_context }}\n\n"
            "{% if final_answer %}"
            "The loop produced this answer:\n{{ final_answer }}\n\n"
            "{% endif %}"
            "Provide a clear, helpful answer to the user based on these results. "
            "Preserve specific details from the loop's answer: names, numbers, "
            "paths, and factual claims must be carried forward exactly. "
            "Be concise and focus on what the user asked for."
        )
        messages = self.prompt_builder.build(
            PromptRequest(
                profile=self.conversation_profile,
                task_content=task_content,
                memory=self.conversation_profile.memory,
                variables={
                    "user_request": user_request,
                    "execution_context": execution_context,
                    "final_answer": final_answer,
                },
            )
        )

        if on_token is not None and hasattr(self.provider, "stream_chat"):
            # Only stream the answer, not the summarizer's <think> reasoning.
            answer_only = _ThinkTagFilter(on_token, keep="outside")
            content = ""
            response = None
            async for event in self.provider.stream_chat(messages):
                if event.type == "token" and event.content:
                    answer_only(event.content)
                    content += event.content
                elif event.type == "done":
                    response = event.response
            return (response.content if response else content) or content
        else:
            response = await self.provider.chat(messages)
            return response.content
