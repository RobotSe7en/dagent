"""Session-scoped state helpers for the harness runtime."""

from __future__ import annotations

import json
from typing import Any

from dagent.harness_runtime.loop_result import LoopResult
from dagent.harness_runtime.review_policy import ReviewLevel
from dagent.harness_runtime.task_record import DirectTaskState, TaskRecord
from dagent.schemas import Boundary


MAX_CONVERSATION_HISTORY_MESSAGES = 20


class HarnessRuntimeSession:
    """Mutable conversation and review state owned by a harness session."""

    def __init__(self, *, tasks: dict[str, TaskRecord]) -> None:
        self.tasks = tasks
        self.conversation_history: list[dict[str, Any]] = []
        self._direct_task_states: dict[str, DirectTaskState] = {}

    def tasks_context(self) -> str:
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

    def record_conversation(self, loop_result: LoopResult) -> None:
        """Record loop messages into conversation history.

        For direct mode the full ToolAgentLoop conversation replaces history.
        For DAG mode there are no conversation messages (DAG has internal state).
        """
        if loop_result.messages:
            self.remember_conversation_messages(loop_result.messages)

    def remember_conversation_messages(self, messages: list[dict[str, Any]]) -> None:
        self.conversation_history = [
            message
            for message in (_conversation_message_copy(item) for item in messages)
            if message is not None
        ][-MAX_CONVERSATION_HISTORY_MESSAGES:]

    def append_conversation_turn(self, user_message: str, assistant_message: str) -> None:
        user_message = user_message.strip()
        assistant_message = assistant_message.strip()
        if user_message and not (
            self.conversation_history
            and self.conversation_history[-1].get("role") == "user"
            and self.conversation_history[-1].get("content") == user_message
        ):
            self.conversation_history.append({"role": "user", "content": user_message})
        if assistant_message:
            self.conversation_history.append({"role": "assistant", "content": assistant_message})
        self.conversation_history = self.conversation_history[-MAX_CONVERSATION_HISTORY_MESSAGES:]

    def store_direct_review_state(
        self,
        user_request: str,
        review_level: ReviewLevel,
        loop_result: LoopResult,
        *,
        boundary: Boundary,
    ) -> None:
        review = loop_result.pending_review
        if review is None or review.kind != "tool_review" or review.tool_call is None:
            return
        self._direct_task_states[review.review_id] = DirectTaskState(
            review_id=review.review_id,
            user_request=user_request,
            messages=loop_result.messages,
            review_level=review_level,
            boundary=boundary,
            tool_call_id=review.tool_call["tool_call_id"],
            tool_name=review.tool_call["name"],
            tool_args=review.tool_call["arguments"],
            risk=review.payload.get("risk", "low"),
        )

    def pop_direct_review_state(self, review_id: str) -> DirectTaskState | None:
        return self._direct_task_states.pop(review_id, None)


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
