"""Top-level harness runtime.

The runtime owns the user-facing loop. It lets the top AgentLoop answer
directly, use ordinary runtime tools, or call the `dag_agent` control tool.
DAG lifecycle state lives in DAGAgentLoop; this module routes UI-facing
messages and summarizes DAG observations back to the user.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from dagent.harness_runtime.agent_loop import (
    AgentLoop,
    ControlToolResult,
    LoopEventHandler,
    TokenHandler,
)
from dagent.harness_runtime.dag_executor import (
    RunResult,
)
from dagent.harness_runtime.dag_agent import DAGAgentLoop, dag_run_fallback_message, _dag_created_review_message
from dagent.harness_runtime.result_reviewer import ResultReviewerAgent, format_review_feedback
from dagent.harness_runtime.auto_mode_tools import DAG_AGENT_NAME, dag_agent_tool_definition
from dagent.harness_runtime.review_policy import ReviewLevel, review_policy
from dagent.harness_runtime.task_record import PendingReview, TaskRecord
from dagent.profiles import AgentProfile
from dagent.providers import ToolCall
from dagent.schemas import Boundary, DAG, TraceEvent
from dagent.state import PromptBuilder, PromptRequest
from dagent.tools.registry import Tool


RuntimeMode = Literal["auto", "direct", "dag"]
MAX_CONVERSATION_HISTORY_MESSAGES = 20


@dataclass(frozen=True)
class HarnessMessageResult:
    status: Literal["completed", "awaiting_dag_review", "awaiting_change_review", "awaiting_approval", "failed"]
    message_markdown: str
    dag: DAG | None = None
    run_result: RunResult | None = None
    task_id: str | None = None
    control_events: list[dict[str, Any]] = field(default_factory=list)
    pending_review: PendingReview | None = None


class HarnessRuntime:
    """Runs top-level messages and manages DAG lifecycle."""

    def __init__(
        self,
        *,
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
        self._active_review_level: ReviewLevel = "fast"
        self._active_user_message: str = ""
        self._active_on_token: TokenHandler | None = None
        self._active_on_event: LoopEventHandler | None = None
        self._conversation_history: list[dict[str, Any]] = []

    async def handle_message(
        self,
        message: str,
        *,
        mode: RuntimeMode = "auto",
        review_level: ReviewLevel = "fast",
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult:
        if mode == "dag":
            loop_result = await self.dag_agent_loop.run(
                message,
                review_level=review_level,
                planning_context=self._conversation_context(),
                runtime_mode="dag",
                dag_messages=self._new_dag_messages(),
                on_token=on_token,
                on_trace=_trace_event_emitter(on_event),
                on_dag=_dag_event_emitter(on_event),
            )
            record = self.tasks.get(loop_result.task_id) if loop_result.task_id else None
            if record is None:
                self._append_conversation_turn(message, loop_result.message_markdown)
                return HarnessMessageResult(
                    status=loop_result.status,
                    message_markdown=loop_result.message_markdown,
                    task_id=loop_result.task_id,
                )
            if loop_result.status == "awaiting_dag_review" and record is not None:
                return HarnessMessageResult(
                    status="awaiting_dag_review",
                    message_markdown=loop_result.message_markdown,
                    dag=loop_result.dag,
                    task_id=loop_result.task_id,
                    pending_review=loop_result.pending_review,
                    control_events=[_dag_event(record, "dag_created", reason="Forced DAG mode.")],
                )
            if loop_result.status == "awaiting_change_review" and record is not None:
                return HarnessMessageResult(
                    status="awaiting_change_review",
                    message_markdown=loop_result.message_markdown,
                    dag=loop_result.dag,
                    run_result=loop_result.run_result,
                    task_id=loop_result.task_id,
                    pending_review=loop_result.pending_review,
                    control_events=[_dag_event(record, "change_review_requested", reason=loop_result.message_markdown)],
                )
            if record is not None:
                loop_result = await self._maybe_review_dag(
                    loop_result, record, message,
                    review_level=review_level,
                    on_token=on_token, on_event=on_event,
                )
                self._append_conversation_turn(record.user_request, loop_result.message_markdown)
                return HarnessMessageResult(
                    status=loop_result.status,
                    message_markdown=loop_result.message_markdown,
                    dag=loop_result.dag,
                    run_result=loop_result.run_result,
                    task_id=loop_result.task_id,
                    control_events=[_dag_event(record, "dag_executed", reason="DAG execution completed.")],
                )
            return HarnessMessageResult(
                status="failed",
                message_markdown=loop_result.message_markdown,
                task_id=loop_result.task_id,
            )

        base_messages = self.prompt_builder.build(
            PromptRequest(
                profile=self.conversation_profile,
                task_content="{{ user_message }}",
                tools=self.runtime_tools,
                memory=self.conversation_profile.memory,
                context=self._conversation_context(),
                variables={"user_message": message},
            )
        )
        system_msg = base_messages[0]
        current_user_msg = base_messages[1]
        messages = [system_msg, *self._conversation_history, current_user_msg]
        include_dag_agent = mode == "auto"
        self._active_review_level = review_level
        self._active_user_message = message
        self._active_on_token = on_token
        self._active_on_event = on_event
        control_names: set[str] = {DAG_AGENT_NAME} if include_dag_agent else set()
        extra_tools = [dag_agent_tool_definition()] if include_dag_agent else None
        has_control = bool(control_names)
        try:
            result = await self.agent_loop.run(
                "",
                boundary=Boundary(mode="read_only", allowed_paths=["."]),
                max_steps=self.max_top_steps,
                messages=messages,
                extra_tools=extra_tools,
                control_tool_names=control_names if has_control else None,
                control_tool_handler=self._handle_control_tool if has_control else None,
                on_token=on_token,
                on_event=on_event,
            )
        finally:
            self._active_user_message = ""
            self._active_on_token = None
            self._active_on_event = None

        self._remember_conversation_messages(result.messages)
        dag_event = _latest_dag_event(result.control_events)
        final_response = result.final_response
        if result.stop_reason != "awaiting_approval" and self.enable_reviewer and self.reviewer:
            dag = dag_event.get("dag") if dag_event else None
            run_result = dag_event.get("run_result") if dag_event else None
            final_response = await self._maybe_review(
                message, final_response,
                dag=dag, run_result=run_result,
            )
        return HarnessMessageResult(
            status="awaiting_dag_review" if result.stop_reason == "awaiting_approval" else "completed",
            message_markdown=final_response,
            dag=dag_event.get("dag") if dag_event else None,
            run_result=dag_event.get("run_result") if dag_event else None,
            task_id=dag_event.get("task_id") if dag_event else None,
            control_events=result.control_events,
            pending_review=dag_event.get("pending_review") if dag_event else None,
        )

    async def resume_dag(
        self,
        task_id: str,
        dag: DAG,
        review_level: ReviewLevel | None = None,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult:
        loop_result = await self.dag_agent_loop.resume(
            task_id,
            dag,
            review_level=review_level,
            on_token=on_token,
            on_trace=_trace_event_emitter(on_event),
            on_dag=_dag_event_emitter(on_event),
        )
        record = self.tasks[task_id]

        if loop_result.status in {"awaiting_dag_review", "awaiting_change_review"}:
            return HarnessMessageResult(
                status=loop_result.status,
                message_markdown=loop_result.message_markdown,
                dag=loop_result.dag,
                run_result=loop_result.run_result,
                task_id=task_id,
                pending_review=loop_result.pending_review,
                control_events=[_dag_event(record, "dag_executed", reason="DAG paused for review.")],
            )

        loop_result = await self._maybe_review_dag(
            loop_result, record, record.user_request,
            review_level=review_level or record.review_level,
            on_token=on_token, on_event=on_event,
        )
        self._append_conversation_turn(record.user_request, loop_result.message_markdown)
        return HarnessMessageResult(
            status=loop_result.status,
            message_markdown=loop_result.message_markdown,
            dag=loop_result.dag,
            run_result=loop_result.run_result,
            task_id=task_id,
            control_events=[_dag_event(record, "dag_executed", reason="User confirmed DAG.")],
        )

    async def _handle_control_tool(self, tool_call: ToolCall) -> ControlToolResult:
        if tool_call.name != DAG_AGENT_NAME:
            raise ValueError(f"Unexpected control tool '{tool_call.name}'.")

        request = str(tool_call.arguments.get("request") or "").strip()
        reason = str(tool_call.arguments.get("reason") or "").strip()
        if not request:
            request = "Create a reviewable DAG for the current user task."

        dag_messages = self._new_dag_messages()
        loop_result = await self.dag_agent_loop.run(
            request,
            review_level=self._active_review_level,
            planning_context=self._conversation_context(),
            runtime_mode="auto",
            dag_messages=dag_messages,
            force_review=review_policy(self._active_review_level).reviews_dag_changes(),
            on_token=self._active_on_token,
            on_trace=_trace_event_emitter(self._active_on_event),
            on_dag=_dag_event_emitter(self._active_on_event),
        )

        record = self.tasks.get(loop_result.task_id) if loop_result.task_id else None
        if record is None:
            return ControlToolResult(
                content=loop_result.message_markdown,
            )
        if self._active_user_message:
            record.user_request = self._active_user_message
        return _control_result_from_dag_loop(
            record,
            loop_result,
            reason=reason,
        )

    def _conversation_context(self) -> str:
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

    def _new_dag_messages(self) -> list[dict[str, Any]]:
        """Create a new dag_messages list seeded with conversation history."""
        messages: list[dict[str, Any]] = []
        for msg in self._conversation_history:
            role = msg.get("role")
            content = msg.get("content")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        active = self._active_user_message.strip()
        if active and not (
            messages
            and messages[-1].get("role") == "user"
            and messages[-1].get("content") == active
        ):
            messages.append({"role": "user", "content": active})
        return messages[-MAX_CONVERSATION_HISTORY_MESSAGES:]

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
    # Result review
    # ------------------------------------------------------------------

    async def _maybe_review(
        self,
        user_request: str,
        final_answer: str,
        *,
        dag: DAG | None = None,
        run_result: RunResult | None = None,
    ) -> str:
        if not self.enable_reviewer or not self.reviewer or not final_answer.strip():
            return final_answer
        execution_context = _format_execution_context(dag, run_result) if run_result else ""
        review = await self.reviewer.review(
            user_request=user_request,
            final_answer=final_answer,
            execution_context=execution_context,
        )
        if review.approved:
            return final_answer
        feedback = format_review_feedback(review)
        return f"{final_answer}\n\n---\n**Reviewer notes:**\n{feedback}"

    async def _maybe_review_dag(
        self,
        loop_result,
        record: TaskRecord,
        user_request: str,
        *,
        review_level: ReviewLevel,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ):
        if not self.enable_reviewer or not self.reviewer or not loop_result.run_result:
            return loop_result
        execution_context = _format_execution_context(loop_result.dag, loop_result.run_result)
        review = await self.reviewer.review(
            user_request=user_request,
            final_answer=loop_result.message_markdown,
            execution_context=execution_context,
        )
        if review.approved:
            return loop_result
        feedback = format_review_feedback(review)
        for _retry in range(self.max_review_retries):
            planning_context = self._conversation_context() + "\n\n" + feedback
            retry_result = await self.dag_agent_loop.run(
                user_request,
                review_level=review_level,
                planning_context=planning_context,
                runtime_mode=record.runtime_mode,
                dag_messages=self._new_dag_messages(),
                on_token=on_token,
                on_trace=_trace_event_emitter(on_event),
                on_dag=_dag_event_emitter(on_event),
            )
            if retry_result.task_id and retry_result.task_id in self.tasks:
                record = self.tasks[retry_result.task_id]
            if not retry_result.run_result:
                return retry_result
            review = await self.reviewer.review(
                user_request=user_request,
                final_answer=retry_result.message_markdown,
                execution_context=_format_execution_context(retry_result.dag, retry_result.run_result),
            )
            if review.approved:
                return retry_result
            feedback = format_review_feedback(review)
        return retry_result


def _format_execution_context(dag: DAG | None, run_result: RunResult | None) -> str:
    lines: list[str] = []
    if dag:
        lines.append(f"DAG ({len(dag.nodes)} nodes, status={dag.status}):")
        for node in dag.nodes:
            lines.append(f"  - {node.id}: {node.tool}({node.args})")
    if run_result and run_result.node_results:
        lines.append("Node execution results:")
        for node_id, node_result in run_result.node_results.items():
            status = "completed" if node_result.completed else "failed"
            response = node_result.final_response.strip()[:500] or node_result.stop_reason
            lines.append(f"  - {node_id} ({status}): {response}")
    return "\n".join(lines) if lines else ""


def _dag_event(
    record: TaskRecord,
    kind: str,
    *,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "task_id": record.task_id,
        "dag": record.dag,
        "reason": reason,
    }


def _latest_dag_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("kind") in {"dag_created", "dag_executed"}:
            return event
    return None


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


def _control_result_from_dag_loop(
    record: TaskRecord,
    loop_result,
    *,
    reason: str,
) -> ControlToolResult:
    if loop_result.status in {"awaiting_dag_review", "awaiting_change_review"}:
        event = _dag_event(
            record,
            "dag_created",
            reason=reason,
        )
        event["pending_review"] = loop_result.pending_review
        return ControlToolResult(
            content=_dag_created_tool_output(record, reason=reason),
            stop_reason="awaiting_approval",
            events=[event],
        )

    event = _dag_event(
        record,
        "dag_executed",
        reason=reason or "DAG executed.",
    )
    event["run_result"] = loop_result.run_result
    content = (
        _dag_run_tool_output(record, loop_result.run_result)
        if loop_result.run_result is not None
        else loop_result.message_markdown
    )
    return ControlToolResult(content=content, events=[event])


def _dag_created_tool_output(record: TaskRecord, *, reason: str) -> str:
    return json.dumps(
        {
            "status": record.dag.status,
            "task_id": record.task_id,
            "reason": reason,
            "dag": record.dag.model_dump(mode="json"),
        },
        ensure_ascii=False,
    )


def _task_context_payload(record: TaskRecord) -> dict[str, Any]:
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


def _dag_run_tool_output(record: TaskRecord, result: RunResult) -> str:
    return json.dumps(
        {
            "status": "completed" if result.completed else "failed",
            "task_id": record.task_id,
            "dag_id": result.dag_id,
            "user_request": record.user_request,
            "instruction": (
                "Summarize the DAG execution result and answer the user's original "
                "request directly. Use the node_results as observations."
            ),
            "node_results": {
                node_id: {
                    "completed": node_result.completed,
                    "stop_reason": node_result.stop_reason,
                    "final_response": node_result.final_response,
                }
                for node_id, node_result in result.node_results.items()
            },
        },
        ensure_ascii=False,
    )


