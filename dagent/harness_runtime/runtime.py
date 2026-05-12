"""Top-level harness runtime.

The runtime orchestrates: route → loop → review → summarize.
It does not know how loops execute internally — it only consumes LoopResult.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from dagent.harness_runtime.agent_loop import (
    AgentLoop,
    LoopEventHandler,
    TokenHandler,
)
from dagent.harness_runtime.dag_executor import RunResult
from dagent.harness_runtime.dag_agent import DAGAgentLoop
from dagent.harness_runtime.loop_result import LoopResult
from dagent.harness_runtime.result_reviewer import ResultReviewerAgent, format_review_feedback
from dagent.harness_runtime.review_policy import ReviewLevel
from dagent.harness_runtime.task_record import DirectTaskState, PendingReview
from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider
from dagent.schemas import Boundary, DAG, TraceEvent
from dagent.state import PromptBuilder, PromptRequest
from dagent.tools.registry import Tool


RuntimeMode = Literal["auto", "direct", "dag"]
MAX_CONVERSATION_HISTORY_MESSAGES = 20

_ROUTE_SYSTEM_PROMPT = """\
You are a routing classifier.  Given the user message and conversation context, \
decide whether the request needs a multi-step DAG pipeline or can be handled by a \
single direct agent loop.

Respond with exactly one word: "dag" or "direct".

Choose "dag" ONLY when the task clearly benefits from node-level planning, \
parallelism, human review gates, resumability, or multi-agent coordination.

Choose "direct" for greetings, simple Q&A, single tool calls, ordinary short \
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


class HarnessRuntime:
    """Orchestrates: route → loop → review → summarize."""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        agent_loop: AgentLoop,
        dag_agent_loop: DAGAgentLoop,
        conversation_profile: AgentProfile,
        runtime_tools: list[Tool] | None = None,
        prompt_builder: PromptBuilder | None = None,
        reviewer: ResultReviewerAgent | None = None,
        enable_reviewer: bool = False,
        max_top_steps: int = 8,
        max_review_retries: int = 1,
    ) -> None:
        self.provider = provider
        self.agent_loop = agent_loop
        self.dag_agent_loop = dag_agent_loop
        self.conversation_profile = conversation_profile
        self.runtime_tools = runtime_tools or []
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.reviewer = reviewer
        self.enable_reviewer = enable_reviewer
        self.max_top_steps = max_top_steps
        self.max_review_retries = max_review_retries
        self.tasks = dag_agent_loop.tasks
        self._conversation_history: list[dict[str, Any]] = []
        self._direct_task_states: dict[str, DirectTaskState] = {}

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

    async def _run_review_cycle(
        self,
        loop_result: LoopResult,
        user_request: str,
        *,
        on_event: LoopEventHandler | None,
    ) -> tuple[bool, str | None]:
        approved = True
        feedback: str | None = None
        if not self.enable_reviewer or not self.reviewer:
            return approved, feedback
        if not loop_result.execution_context.strip():
            return approved, feedback
        if on_event:
            on_event({"type": "reviewing", "message": "Reviewing result quality..."})
        review = await self.reviewer.review(
            user_request=user_request,
            final_answer=loop_result.final_answer,
            execution_context=loop_result.execution_context,
        )
        if review.approved:
            if on_event:
                on_event({
                    "type": "review_passed",
                    "approved": True,
                    "summary": review.summary,
                    "issues": [
                        {"message": issue.message, "node_id": issue.node_id}
                        for issue in review.issues
                    ],
                })
            return approved, feedback
        feedback = format_review_feedback(review)
        approved = False
        if on_event:
            on_event({
                "type": "retry",
                "reason": feedback,
                "summary": review.summary,
                "issues": [
                    {"message": issue.message, "node_id": issue.node_id}
                    for issue in review.issues
                ],
            })
        return approved, feedback

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
            self._append_conversation_turn(user_request, summary)
        return HarnessMessageResult(
            status="completed" if loop_result.completed else "failed",
            message_markdown=summary,
            dag=loop_result.dag,
            run_result=loop_result.run_result,
            task_id=loop_result.task_id,
            events=loop_result.events,
            pending_review=loop_result.pending_review,
        )

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

        # 2. Loop + Review cycle
        feedback: str | None = None
        loop_result: LoopResult | None = None
        prior_messages: list[dict[str, Any]] | None = None

        for _attempt in range(self.max_review_retries + 1):
            loop_result = await self._execute_loop(
                message,
                mode=resolved_mode,
                review_level=review_level,
                feedback=feedback,
                prior_messages=prior_messages,
                on_token=on_token,
                on_event=on_event,
            )

            gate = self._human_review_gate(loop_result)
            if gate is not None:
                if loop_result.pending_review and loop_result.pending_review.kind == "tool_review":
                    review = loop_result.pending_review
                    self._direct_task_states[review.review_id] = DirectTaskState(
                        review_id=review.review_id,
                        messages=loop_result.messages,
                        review_level=review_level,
                        boundary=Boundary(mode="read_only", allowed_paths=["."]),
                        tool_call_id=review.tool_call["tool_call_id"],
                        tool_name=review.tool_call["name"],
                        tool_args=review.tool_call["arguments"],
                        risk=review.payload.get("risk", "low"),
                    )
                return gate

            approved, review_feedback = await self._run_review_cycle(
                loop_result,
                message,
                on_event=on_event,
            )
            if approved:
                break
            feedback = review_feedback
            prior_messages = loop_result.messages

            assert loop_result is not None

        self._record_conversation(loop_result)
        return await self._build_summary_result(loop_result, message, on_token=on_token)

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

        dag_result = await self.dag_agent_loop.resume(
            task_id,
            dag,
            review_level=review_level,
            on_token=on_token,
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

        feedback: str | None = None
        for _attempt in range(self.max_review_retries + 1):
            if feedback is not None:
                retry_dag_result = await self.dag_agent_loop.run(
                    record.user_request,
                    review_level=review_level or record.review_level,
                    planning_context=self._tasks_context() + "\n\n" + feedback,
                    runtime_mode=record.runtime_mode,
                    conversation_history=self._conversation_history,
                    on_token=on_token,
                    on_trace=_trace_event_emitter(on_event),
                    on_dag=_dag_event_emitter(on_event),
                )
                loop_result = retry_dag_result.to_loop_result()
                gate = self._human_review_gate(loop_result)
                if gate is not None:
                    return gate
                if not loop_result.execution_context.strip():
                    break

            approved, review_feedback = await self._run_review_cycle(
                loop_result,
                record.user_request,
                on_event=on_event,
            )
            if approved:
                break
            feedback = review_feedback

        return await self._build_summary_result(loop_result, record.user_request, on_token=on_token)

    # ==================================================================
    # Route
    # ==================================================================

    async def _route(self, message: str) -> Literal["dag", "direct"]:
        """Lightweight LLM classifier."""
        context = self._tasks_context()
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
            if decision in {"dag", "direct"}:
                return decision  # type: ignore[return-value]
        except Exception:
            pass
        return "direct"

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
            planning_context = self._tasks_context()
            if feedback:
                planning_context = (planning_context + "\n\n" + feedback) if planning_context else feedback
            thinking_only = _ThinkTagFilter(on_token, keep="inside") if on_token else None
            dag_result = await self.dag_agent_loop.run(
                message,
                review_level=review_level,
                planning_context=planning_context,
                runtime_mode=str(mode),
                conversation_history=self._conversation_history,
                on_token=thinking_only,
                on_trace=_trace_event_emitter(on_event),
                on_dag=_dag_event_emitter(on_event),
            )
            return dag_result.to_loop_result()
        else:
            # Direct mode: only stream <think> blocks from the loop.
            # The actual answer comes from _summarize().
            thinking_only = _ThinkTagFilter(on_token, keep="inside") if on_token else None
            return await self._run_direct(
                message,
                review_level=review_level,
                feedback=feedback,
                prior_messages=prior_messages,
                on_token=thinking_only,
                on_event=on_event,
            )

    async def _run_direct(
        self,
        message: str,
        *,
        review_level: ReviewLevel = "fast",
        feedback: str | None = None,
        prior_messages: list[dict[str, Any]] | None = None,
        on_token: TokenHandler | None,
        on_event: LoopEventHandler | None,
    ) -> LoopResult:
        """Run AgentLoop and return unified LoopResult."""
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
                    context=self._tasks_context(),
                    variables={"user_message": message},
                )
            )
            system_msg = base_messages[0]
            current_user_msg = base_messages[1]
            messages = [system_msg, *self._conversation_history, current_user_msg]

        boundary = Boundary(mode="read_only", allowed_paths=["."])
        control_tool_names = {t.name for t in self.runtime_tools if t.risk in {"medium", "high"} or t.risk_fn is not None}
        result = await self.agent_loop.run(
            "",
            boundary=boundary,
            max_steps=self.max_top_steps,
            messages=messages,
            control_tool_names=control_tool_names,
            control_tool_handler=self.agent_loop.create_tool_guard(review_level, boundary, self.runtime_tools),
            on_token=on_token,
            on_event=on_event,
        )
        return result.to_loop_result()

    async def resume_direct(
        self,
        review_id: str,
        approved: bool,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult | None:
        state = self._direct_task_states.pop(review_id, None)
        if state is None:
            return None

        feed_content = "[DENIED] Tool '{name}' was rejected by the user. Do not retry this exact tool call.".format(name=state.tool_name)
        if approved:
            try:
                feed_content = self.agent_loop.tool_executor.execute(
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

        thinking_only = _ThinkTagFilter(on_token, keep="inside") if on_token else None
        result = await self.agent_loop.run(
            "",
            boundary=state.boundary,
            max_steps=self.max_top_steps,
            messages=state.messages,
            control_tool_names={t.name for t in self.runtime_tools if t.risk in {"medium", "high"} or t.risk_fn is not None},
            control_tool_handler=self.agent_loop.create_tool_guard(state.review_level, state.boundary, self.runtime_tools),
            on_token=thinking_only,
            on_event=on_event,
        )
        loop_result = result.to_loop_result()

        from dagent.harness_runtime.agent_loop import _format_direct_execution_context
        loop_result = LoopResult(
            execution_context=_format_direct_execution_context(loop_result.messages),
            final_answer=loop_result.final_answer,
            messages=loop_result.messages,
            completed=loop_result.completed,
            needs_human_review=loop_result.needs_human_review,
            pending_review=loop_result.pending_review,
        )

        gate = self._human_review_gate(loop_result)
        if gate is not None:
            return gate

        await self._run_review_cycle(loop_result, "(resumed from tool review)", on_event=on_event)
        return await self._build_summary_result(loop_result, "(resumed from tool review)", on_token=on_token)

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
            "Preserve specific details from the loop's answer — names, numbers, "
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

    def _record_conversation(self, loop_result: LoopResult) -> None:
        """Record loop messages into conversation history.

        For direct mode the full AgentLoop conversation replaces history.
        For DAG mode there are no conversation messages (DAG has internal state).
        """
        if loop_result.messages:
            self._remember_conversation_messages(loop_result.messages)

    # ==================================================================
    # Conversation history
    # ==================================================================

    def _tasks_context(self) -> str:
        if not self.tasks:
            return ""
        payload = {
            "recent_dag_tasks": [
                _task_context_payload(record)
                for record in list(self.tasks.values())[-3:]
            ]
        }
        return (
            "Recent DAG execution context is available. Use it to interpret "
            "follow-up requests and DAG planning requests; do not repeat work "
            "whose result is already available unless the user asks to rerun it.\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )

    def _remember_conversation_messages(self, messages: list[dict[str, Any]]) -> None:
        self._conversation_history = [
            message
            for message in (_conversation_message_copy(item) for item in messages)
            if message is not None
        ][-MAX_CONVERSATION_HISTORY_MESSAGES:]

    def _append_conversation_turn(self, user_message: str, assistant_message: str) -> None:
        user_message = user_message.strip()
        assistant_message = assistant_message.strip()
        if user_message and not (
            self._conversation_history
            and self._conversation_history[-1].get("role") == "user"
            and self._conversation_history[-1].get("content") == user_message
        ):
            self._conversation_history.append({"role": "user", "content": user_message})
        if assistant_message:
            self._conversation_history.append({"role": "assistant", "content": assistant_message})
        self._conversation_history = self._conversation_history[-MAX_CONVERSATION_HISTORY_MESSAGES:]


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

class _ThinkTagFilter:
    """Streaming filter that selectively forwards tokens based on <think> blocks.

    Two modes (controlled by ``keep``):

    * ``keep="inside"`` — forward only tokens *inside* ``<think>…</think>``
      (including the tags themselves).  Used during AgentLoop execution so the
      user sees the model's reasoning but not the answer (which will come from
      ``_summarize()``).

    * ``keep="outside"`` — forward only tokens *outside* ``<think>…</think>``.
      Used during ``_summarize()`` so the user sees the final answer but not
      the summarizer's internal reasoning.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self, downstream: TokenHandler, *, keep: str = "inside") -> None:
        assert keep in {"inside", "outside"}
        self._downstream = downstream
        self._keep = keep
        self._buf = ""
        self._inside = False

    def _should_emit(self) -> bool:
        return (self._keep == "inside") == self._inside

    def __call__(self, token: str) -> None:
        self._buf += token
        while self._buf:
            if not self._inside:
                idx = self._buf.lower().find(self._OPEN)
                if idx == -1:
                    # No opening tag.  Keep a small tail in case a tag is
                    # split across tokens, but only if '<' is present in
                    # the tail — otherwise the tail cannot start a tag.
                    keep = len(self._OPEN) - 1
                    if len(self._buf) > keep and "<" not in self._buf[-keep:]:
                        # No chance of a partial tag — flush everything.
                        if self._should_emit():
                            self._downstream(self._buf)
                        self._buf = ""
                    else:
                        safe = self._buf[:-keep] if len(self._buf) > keep else ""
                        if safe and self._should_emit():
                            self._downstream(safe)
                        self._buf = self._buf[len(safe):]
                    return
                # Text before <think>
                before = self._buf[:idx]
                if before and self._should_emit():
                    self._downstream(before)
                # The tag itself — emit if keeping inside
                tag_end = idx + len(self._OPEN)
                self._inside = True
                if self._should_emit():
                    self._downstream(self._buf[idx:tag_end])
                self._buf = self._buf[tag_end:]
            else:
                idx = self._buf.lower().find(self._CLOSE)
                if idx == -1:
                    safe_len = len(self._buf) - (len(self._CLOSE) - 1)
                    if safe_len > 0 and "<" not in self._buf[-max(len(self._CLOSE) - 1, 0):]:
                        # No partial close tag possible — flush all.
                        if self._should_emit():
                            self._downstream(self._buf)
                        self._buf = ""
                    elif safe_len > 0:
                        safe = self._buf[:safe_len]
                        if self._should_emit():
                            self._downstream(safe)
                        self._buf = self._buf[safe_len:]
                    return
                # Text inside think + closing tag
                tag_end = idx + len(self._CLOSE)
                inside_text = self._buf[:idx]
                if inside_text and self._should_emit():
                    self._downstream(inside_text)
                # Closing tag — about to leave inside
                if self._should_emit():
                    self._downstream(self._buf[idx:tag_end])
                self._buf = self._buf[tag_end:]
                self._inside = False


def _trace_event_emitter(on_event: LoopEventHandler | None):
    if on_event is None:
        return None

    def emit(trace: TraceEvent) -> None:
        on_event({"type": "trace", "event": trace.model_dump(mode="json")})

    return emit


def _dag_event_emitter(on_event: LoopEventHandler | None):
    if on_event is None:
        return None

    def emit(dag: DAG) -> None:
        on_event({"type": "dag", "dag": dag.model_dump(mode="json")})

    return emit


def _task_context_payload(record) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "dag_id": record.dag.dag_id,
        "user_request": record.user_request,
        "dag_status": record.dag.status,
        "node_results": {
            node_id: {
                "completed": node_result.completed,
                "stop_reason": node_result.stop_reason,
                "final_response": node_result.final_response,
            }
            for node_id, node_result in record.node_results.items()
        },
        "recent_trace_records": [
            {
                "node_id": trace.node_id,
                "tool": trace.tool,
                "status": trace.status,
                "output": trace.output,
                "error": trace.error,
            }
            for trace in record.trace_records[-8:]
        ],
    }


def _conversation_message_copy(message: dict[str, Any]) -> dict[str, Any] | None:
    role = message.get("role")
    if role not in {"user", "assistant"}:
        return None
    content = message.get("content")
    if not content:
        return None
    return {"role": role, "content": content}
