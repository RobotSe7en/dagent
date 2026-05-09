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
from uuid import uuid4

from dagent.harness_runtime.agent_loop import (
    AgentLoop,
    ControlToolResult,
    LoopEventHandler,
    TokenHandler,
)
from dagent.harness_runtime.dag_executor import (
    RunResult,
)
from dagent.harness_runtime.dag_agent import DAGAgentLoop
from dagent.harness_runtime.auto_mode_tools import DAG_AGENT_NAME, dag_agent_tool_definition
from dagent.harness_runtime.review_policy import ReviewLevel, review_policy
from dagent.harness_runtime.task_record import PendingReview, TaskRecord
from dagent.profiles import AgentProfile
from dagent.providers import ToolCall
from dagent.schemas import Boundary, DAG
from dagent.state import PromptBuilder, PromptRequest
from dagent.tools.registry import Tool


RuntimeMode = Literal["auto", "direct", "dag"]
MAX_CONVERSATION_HISTORY_MESSAGES = 20


@dataclass
class PendingToolReview:
    review_id: str
    tool_call: ToolCall
    messages: list[dict[str, Any]]
    mode: RuntimeMode
    review_level: ReviewLevel
    remaining_steps: int


@dataclass(frozen=True)
class HarnessMessageResult:
    status: Literal["completed", "awaiting_dag_review", "awaiting_change_review", "awaiting_approval", "awaiting_tool_review", "failed"]
    message_markdown: str
    dag: DAG | None = None
    run_result: RunResult | None = None
    task_id: str | None = None
    control_events: list[dict[str, Any]] = field(default_factory=list)
    pending_review: PendingReview | None = None
    pending_tool_review: PendingToolReview | None = None


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
        max_top_steps: int = 8,
    ) -> None:
        self.agent_loop = agent_loop
        self.dag_agent_loop = dag_agent_loop
        self.conversation_profile = conversation_profile
        self.runtime_tools = runtime_tools or []
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.max_top_steps = max_top_steps
        self.tasks = dag_agent_loop.tasks
        self.runs = dag_agent_loop.runs
        self._active_review_level: ReviewLevel = "balanced"
        self._active_user_message: str = ""
        self._active_continuation_task_id: str | None = None
        self._pending_tool_review: PendingToolReview | None = None
        self._conversation_history: list[dict[str, Any]] = []

    async def handle_message(
        self,
        message: str,
        *,
        mode: RuntimeMode = "auto",
        review_level: ReviewLevel = "balanced",
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
            )
            record = self.tasks[loop_result.task_id] if loop_result.task_id else None
            if loop_result.status == "completed" and record is not None and loop_result.run_result is not None:
                return await self._continue_dag_loop(
                    record,
                    loop_result.run_result,
                    on_token=on_token,
                    on_event=on_event,
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
            if loop_result.status == "failed" and record is not None:
                return HarnessMessageResult(
                    status="failed",
                    message_markdown=loop_result.message_markdown,
                    dag=loop_result.dag,
                    run_result=loop_result.run_result,
                    task_id=loop_result.task_id,
                    pending_review=loop_result.pending_review,
                    control_events=[_dag_event(record, "dag_executed", reason="DAG execution failed.")],
                )
            assert record is not None
            return HarnessMessageResult(
                status="awaiting_dag_review",
                message_markdown=loop_result.message_markdown,
                dag=loop_result.dag,
                task_id=loop_result.task_id,
                control_events=[_dag_event(record, "dag_created", reason="Forced DAG mode.")],
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
        policy = review_policy(review_level)
        reviewed_tool_names = {
            tool.name
            for tool in self.runtime_tools
            if policy.requires_tool_review(tool.risk)
        }
        control_names: set[str] = set(reviewed_tool_names)
        if include_dag_agent:
            control_names.add(DAG_AGENT_NAME)
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

        if result.stop_reason == "pending_tool_review" and self._pending_tool_review is not None:
            self._pending_tool_review.messages = result.messages
            self._pending_tool_review.mode = mode
            self._pending_tool_review.remaining_steps = max(1, self.max_top_steps - result.steps)
            ptr = self._pending_tool_review
            return HarnessMessageResult(
                status="awaiting_tool_review",
                message_markdown=f"Tool `{ptr.tool_call.name}` requires approval before execution.",
                pending_tool_review=ptr,
            )

        self._remember_conversation_messages(result.messages)
        dag_event = _latest_dag_event(result.control_events)
        return HarnessMessageResult(
            status="awaiting_dag_review" if result.stop_reason == "awaiting_approval" else "completed",
            message_markdown=result.final_response,
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
        loop_result = await self.dag_agent_loop.resume(task_id, dag, review_level=review_level)
        record = self.tasks[task_id]
        if (
            loop_result.status == "completed"
            and record.dag.status == "completed"
            and record.message_markdown
        ):
            return HarnessMessageResult(
                status="completed",
                message_markdown=record.message_markdown,
                dag=loop_result.dag,
                run_result=loop_result.run_result,
                task_id=task_id,
            )
        if loop_result.status == "awaiting_change_review":
            return HarnessMessageResult(
                status="awaiting_change_review",
                message_markdown=loop_result.message_markdown,
                dag=loop_result.dag,
                run_result=loop_result.run_result,
                task_id=task_id,
                pending_review=loop_result.pending_review,
                control_events=[_dag_event(record, "change_review_requested", reason=loop_result.message_markdown)],
            )
        if record.runtime_mode == "dag":
            if loop_result.run_result is None or not loop_result.run_result.completed:
                return HarnessMessageResult(
                    status="awaiting_change_review" if loop_result.pending_review else "failed",
                    message_markdown=loop_result.message_markdown,
                    dag=loop_result.dag,
                    run_result=loop_result.run_result,
                    task_id=task_id,
                    pending_review=loop_result.pending_review,
                    control_events=[_dag_event(record, "dag_executed", reason="DAG execution paused before completion.")],
                )
            return await self._continue_dag_loop(
                record,
                loop_result.run_result,
                on_token=on_token,
                on_event=on_event,
            )
        assert loop_result.run_result is not None
        summary = await self._summarize_dag_run(record, loop_result.run_result, on_token=on_token, on_event=on_event)
        record.message_markdown = summary
        self._append_conversation_turn(record.user_request, summary)
        return HarnessMessageResult(
            status="completed" if loop_result.run_result.completed else "failed",
            message_markdown=summary,
            dag=record.dag,
            run_result=loop_result.run_result,
            task_id=task_id,
            control_events=[_dag_event(record, "dag_executed", reason="User confirmed DAG.")],
        )

    def _handle_tool_review(self, tool_call: ToolCall) -> ControlToolResult:
        tool = self.agent_loop.tool_executor.registry.get(tool_call.name)
        risk = tool.risk if tool else "unknown"
        self._pending_tool_review = PendingToolReview(
            review_id=f"tool_review_{uuid4().hex}",
            tool_call=tool_call,
            messages=[],
            mode="auto",
            review_level=self._active_review_level,
            remaining_steps=self.max_top_steps,
        )
        return ControlToolResult(
            content=f"[PENDING_REVIEW] Tool '{tool_call.name}' (risk={risk}) is awaiting human approval. Execution paused.",
            stop_reason="pending_tool_review",
        )

    async def resume_tool_review(
        self,
        approved: bool,
        *,
        review_level: ReviewLevel | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult:
        ptr = self._pending_tool_review
        if ptr is None:
            raise KeyError("No pending tool review.")

        self._pending_tool_review = None
        if review_level is not None:
            ptr.review_level = review_level

        messages = list(ptr.messages)
        if not approved:
            denied_content = "[DENIED] The user denied this tool call. Try an alternative approach or ask the user for guidance."
            _replace_pending_tool_message(messages, ptr.tool_call.id, denied_content)
            _emit_tool_review_event(on_event, ptr.tool_call, "tool_error", denied_content)
            policy = review_policy(ptr.review_level)
            reviewed_tool_names = {
                tool.name
                for tool in self.runtime_tools
                if policy.requires_tool_review(tool.risk)
            }
            include_dag_agent = ptr.mode == "auto"
            control_names: set[str] = set(reviewed_tool_names)
            if include_dag_agent:
                control_names.add(DAG_AGENT_NAME)
            has_control = bool(control_names)
            self._active_review_level = ptr.review_level
            try:
                result = await self.agent_loop.run(
                    "",
                    boundary=Boundary(mode="read_only", allowed_paths=["."]),
                    max_steps=ptr.remaining_steps,
                    messages=messages,
                    extra_tools=[dag_agent_tool_definition()] if include_dag_agent else None,
                    control_tool_names=control_names if has_control else None,
                    control_tool_handler=self._handle_control_tool if has_control else None,
                    on_token=on_token,
                    on_event=on_event,
                )
            finally:
                self._active_user_message = ""

            if result.stop_reason == "pending_tool_review" and self._pending_tool_review is not None:
                self._pending_tool_review.messages = result.messages
                self._pending_tool_review.mode = ptr.mode
                self._pending_tool_review.remaining_steps = max(1, ptr.remaining_steps - result.steps)
                new_ptr = self._pending_tool_review
                return HarnessMessageResult(
                    status="awaiting_tool_review",
                    message_markdown=f"Tool `{new_ptr.tool_call.name}` requires approval before execution.",
                    pending_tool_review=new_ptr,
                )
            self._remember_conversation_messages(result.messages)
            return HarnessMessageResult(
                status="completed",
                message_markdown=result.final_response,
            )

        is_error = False
        try:
            tool_result = self.agent_loop.tool_executor.execute(
                ptr.tool_call.name,
                ptr.tool_call.arguments,
                boundary=Boundary(mode="full", allowed_paths=["."]),
            )
        except Exception as exc:
            tool_result = f"[TOOL_ERROR] {type(exc).__name__}: {exc}"
            is_error = True

        _replace_pending_tool_message(messages, ptr.tool_call.id, tool_result)
        _emit_tool_review_event(
            on_event, ptr.tool_call,
            "tool_error" if is_error else "tool_result",
            tool_result,
        )

        policy = review_policy(ptr.review_level)
        reviewed_tool_names = {
            tool.name
            for tool in self.runtime_tools
            if policy.requires_tool_review(tool.risk)
        }
        include_dag_agent = ptr.mode == "auto"
        control_names = set(reviewed_tool_names)
        if include_dag_agent:
            control_names.add(DAG_AGENT_NAME)
        has_control = bool(control_names)
        self._active_review_level = ptr.review_level
        try:
            result = await self.agent_loop.run(
                "",
                boundary=Boundary(mode="read_only", allowed_paths=["."]),
                max_steps=ptr.remaining_steps,
                messages=messages,
                extra_tools=[dag_agent_tool_definition()] if include_dag_agent else None,
                control_tool_names=control_names if has_control else None,
                control_tool_handler=self._handle_control_tool if has_control else None,
                on_token=on_token,
                on_event=on_event,
            )
        finally:
            self._active_user_message = ""

        if result.stop_reason == "pending_tool_review" and self._pending_tool_review is not None:
            self._pending_tool_review.messages = result.messages
            self._pending_tool_review.mode = ptr.mode
            self._pending_tool_review.remaining_steps = max(1, ptr.remaining_steps - result.steps)
            new_ptr = self._pending_tool_review
            return HarnessMessageResult(
                status="awaiting_tool_review",
                message_markdown=f"Tool `{new_ptr.tool_call.name}` requires approval before execution.",
                pending_tool_review=new_ptr,
            )

        self._remember_conversation_messages(result.messages)
        dag_event = _latest_dag_event(result.control_events)
        return HarnessMessageResult(
            status="awaiting_dag_review" if result.stop_reason == "awaiting_approval" else "completed",
            message_markdown=result.final_response,
            dag=dag_event.get("dag") if dag_event else None,
            run_result=dag_event.get("run_result") if dag_event else None,
            task_id=dag_event.get("task_id") if dag_event else None,
            control_events=result.control_events,
            pending_review=dag_event.get("pending_review") if dag_event else None,
        )

    async def _handle_control_tool(self, tool_call: ToolCall) -> ControlToolResult:
        if tool_call.name != DAG_AGENT_NAME:
            return self._handle_tool_review(tool_call)

        request = str(tool_call.arguments.get("request") or "").strip()
        reason = str(tool_call.arguments.get("reason") or "").strip()
        if not request:
            request = "Create a reviewable DAG for the current user task."

        if self._active_continuation_task_id is not None:
            prior_record = self.tasks[self._active_continuation_task_id]
            dag_messages = prior_record.dag_messages
            prior_results = dict(prior_record.node_results)
            prior_continuation_count = prior_record.continuation_count
            loop_result = await self.dag_agent_loop.run(
                request,
                task_id=prior_record.task_id,
                review_level=prior_record.review_level,
                planning_context=self._conversation_context(),
                runtime_mode=prior_record.runtime_mode,
                dag_messages=dag_messages,
                force_review=True,
            )
            assert loop_result.task_id is not None
            record = self.tasks[loop_result.task_id]
            record.node_results = prior_results
            record.continuation_count = prior_continuation_count + 1
            for node in record.dag.nodes:
                record.node_results.pop(node.id, None)
            event = _dag_event(record, "dag_created", reason=reason or "DAG continuation requested.")
            return ControlToolResult(
                content=_dag_created_tool_output(record, reason=reason),
                stop_reason="awaiting_approval",
                events=[event],
            )

        dag_messages = self._new_dag_messages()
        loop_result = await self.dag_agent_loop.run(
            request,
            review_level=self._active_review_level,
            planning_context=self._conversation_context(),
            runtime_mode="auto",
            dag_messages=dag_messages,
            force_review=True,
        )
        assert loop_result.task_id is not None
        record = self.tasks[loop_result.task_id]
        if self._active_user_message:
            record.user_request = self._active_user_message
        event = _dag_event(record, "dag_created", reason=reason)
        return ControlToolResult(
            content=_dag_created_tool_output(record, reason=reason),
            stop_reason="awaiting_approval",
            events=[event],
        )

    async def _continue_dag_loop(
        self,
        record: TaskRecord,
        result: RunResult,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> HarnessMessageResult:
        messages = self.prompt_builder.build(
            PromptRequest(
                profile=self.conversation_profile,
                task_content="{{ user_message }}",
                tools=[],
                memory=self.conversation_profile.memory,
                context=self._conversation_context(),
                variables={
                    "user_message": (
                        "A DAG segment has finished executing in forced DAG mode.\n"
                        f"Original user request:\n{record.user_request}\n\n"
                        f"DAG execution observation:\n{_dag_run_tool_output(record, result)}\n\n"
                        "Decide the next step. If the observations are sufficient, answer the user. "
                        "If more execution is required, call dag_agent to create the next DAG segment. "
                        "Do not call ordinary tools directly in DAG mode."
                    )
                },
            )
        )
        self._active_review_level = record.review_level
        self._active_user_message = record.user_request
        self._active_continuation_task_id = record.task_id
        try:
            loop_result = await self.agent_loop.run(
                "",
                boundary=Boundary(mode="read_only", allowed_paths=["."]),
                allowed_tools=[],
                max_steps=self.max_top_steps,
                messages=messages,
                extra_tools=[dag_agent_tool_definition()],
                control_tool_names={DAG_AGENT_NAME},
                control_tool_handler=self._handle_control_tool,
                on_token=on_token,
                on_event=on_event,
            )
        finally:
            self._active_continuation_task_id = None
            self._active_user_message = ""

        dag_event = _latest_dag_event(loop_result.control_events)
        if dag_event is not None and loop_result.stop_reason == "awaiting_approval":
            current_record = self.tasks[record.task_id]
            return HarnessMessageResult(
                status="awaiting_dag_review",
                message_markdown=_dag_created_review_message(current_record),
                dag=dag_event.get("dag"),
                run_result=result,
                task_id=record.task_id,
                control_events=[*loop_result.control_events, _dag_event(record, "dag_executed", reason="DAG segment executed.")],
            )

        answer = loop_result.final_response.strip() or _dag_run_fallback_message(record, result)
        record.message_markdown = answer
        self._append_conversation_turn(record.user_request, answer)
        return HarnessMessageResult(
            status="completed" if result.completed else "failed",
            message_markdown=answer,
            dag=record.dag,
            run_result=result,
            task_id=record.task_id,
            control_events=[*loop_result.control_events, _dag_event(record, "dag_executed", reason="DAG segment executed.")],
        )

    async def _summarize_dag_run(
        self,
        record: TaskRecord,
        result: RunResult,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> str:
        messages = self.prompt_builder.build(
            PromptRequest(
                profile=self.conversation_profile,
                task_content="{{ user_message }}",
                tools=self.runtime_tools,
                memory=self.conversation_profile.memory,
                variables={
                    "user_message": (
                        "The user confirmed a DAG and it has been executed.\n"
                        f"Original request:\n{record.user_request}\n\n"
                        f"DAG execution observation:\n{_dag_run_tool_output(record, result)}\n\n"
                        "Answer the user's original request directly."
                    )
                },
            )
        )
        stream_kwargs: dict[str, Any] = {}
        if on_token is not None:
            stream_kwargs["on_token"] = on_token
        if on_event is not None:
            stream_kwargs["on_event"] = on_event
        try:
            summary = await asyncio.wait_for(
                self.agent_loop.run(
                    "",
                    boundary=Boundary(mode="read_only", allowed_paths=["."]),
                    max_steps=self.max_top_steps,
                    messages=messages,
                    **stream_kwargs,
                ),
                timeout=60,
            )
        except Exception:
            return _dag_run_fallback_message(record, result)
        return summary.final_response.strip() or _dag_run_fallback_message(record, result)

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


def _dag_created_review_message(record: TaskRecord) -> str:
    return "\n".join(
        [
            "### DAG ready for review",
            f"- **Task:** `{record.task_id}`",
            f"- **Status:** `{record.dag.status}`",
            f"- **Nodes:** {len(record.dag.nodes)}",
            "- **Next action:** Review and edit the DAG, then confirm to resume execution.",
        ]
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


def _dag_run_fallback_message(record: TaskRecord, result: RunResult) -> str:
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


def _emit_tool_review_event(
    on_event: Any,
    tool_call: ToolCall,
    event_type: str,
    content: str,
) -> None:
    if on_event is None:
        return
    on_event({
        "type": event_type,
        "tool_call_id": tool_call.id,
        "name": tool_call.name,
        "arguments": tool_call.arguments,
        "content": content,
    })


def _replace_pending_tool_message(
    messages: list[dict[str, Any]],
    tool_call_id: str,
    content: str,
) -> None:
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
            messages[i] = {**msg, "content": content}
            return
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content,
    })
