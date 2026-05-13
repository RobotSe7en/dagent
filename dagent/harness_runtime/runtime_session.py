"""Session-scoped state helpers for the harness runtime."""

from __future__ import annotations

import json
from typing import Any

from dagent.harness_runtime.loop_result import LoopResult
from dagent.harness_runtime.review_policy import ReviewLevel
from dagent.harness_runtime.task_record import (
    DAGTaskState,
    ReviewContinuation,
    RuntimeTaskMode,
    RuntimeTaskRecord,
    RuntimeTaskStatus,
    ToolTaskState,
)
from dagent.schemas import Boundary, ToolInvocation


MAX_CONVERSATION_HISTORY_MESSAGES = 20


class HarnessRuntimeSession:
    """Mutable conversation and review state owned by a harness session."""

    def __init__(self, *, tasks: dict[str, RuntimeTaskRecord]) -> None:
        self.tasks = tasks
        self.conversation_history: list[dict[str, Any]] = []
        self.runtime_tasks = tasks
        self._review_continuations: dict[str, ReviewContinuation] = {}

    def tasks_context(self) -> str:
        if not self.tasks:
            return ""
        payload = {
            "recent_dag_tasks": [
                _task_context_payload(record)
                for record in list(self.runtime_tasks.values())[-3:]
                if record.dag_state is not None
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

        For tool mode the full ToolAgentLoop conversation replaces history.
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

    def store_review_continuation(
        self,
        *,
        task_id: str,
        user_request: str,
        review_level: ReviewLevel,
        loop_result: LoopResult,
        boundary: Boundary,
    ) -> None:
        review = loop_result.pending_review
        if review is None:
            return
        tool_call = review.tool_call or {}
        self._review_continuations[review.review_id] = ReviewContinuation(
            review_id=review.review_id,
            task_id=task_id,
            kind=review.kind,
            user_request=user_request,
            messages=loop_result.messages,
            invocations=loop_result.invocations,
            review_level=review_level,
            boundary=boundary,
            tool_call_id=tool_call.get("tool_call_id"),
            tool_name=tool_call.get("name"),
            tool_args=tool_call.get("arguments", {}),
            risk=review.payload.get("risk", "low"),
        )

    def pop_review_continuation(self, review_id: str) -> ReviewContinuation | None:
        return self._review_continuations.pop(review_id, None)

    def discard_review_continuations_for_task(self, task_id: str) -> None:
        stale_review_ids = [
            review_id
            for review_id, continuation in self._review_continuations.items()
            if continuation.task_id == task_id
        ]
        for review_id in stale_review_ids:
            self._review_continuations.pop(review_id, None)

    def record_runtime_task(
        self,
        *,
        task_id: str,
        mode: RuntimeTaskMode,
        user_request: str,
        review_level: ReviewLevel,
        status: RuntimeTaskStatus,
        loop_result: LoopResult,
        invocations: list[ToolInvocation] | None = None,
    ) -> RuntimeTaskRecord:
        existing = self.runtime_tasks.get(task_id)
        execution_records = list(existing.trace_records) if existing and existing.dag_state else []
        task_invocations = invocations if invocations is not None else loop_result.invocations
        record = existing or (
            RuntimeTaskRecord.dag_task(
                task_id=task_id,
                user_request=user_request,
                dag=loop_result.dag,
                review_level=review_level,
            )
            if mode == "dag" and loop_result.dag is not None
            else RuntimeTaskRecord.tool_task(
                task_id=task_id,
                user_request=user_request,
                review_level=review_level,
            )
        )
        record.status = status
        record.review_level = review_level
        record.pending_review = loop_result.pending_review
        record.final_response = loop_result.final_answer
        record.invocations = {
            invocation.invocation_id: invocation
            for invocation in task_invocations
        }
        record.execution_records = execution_records
        if loop_result.dag is not None:
            if record.dag_state is None:
                record.dag_state = DAGTaskState(dag=loop_result.dag)
            record.dag_state.dag = loop_result.dag
            if loop_result.run_result and loop_result.run_result not in record.dag_state.runs:
                record.dag_state.runs.append(loop_result.run_result)
            if loop_result.run_result:
                record.dag_state.node_results = loop_result.run_result.node_results
            record.dag_state.trace_records = execution_records
        else:
            previous_boundary = (
                record.tool_state.boundary
                if record.tool_state is not None
                else Boundary(mode="read_only", allowed_paths=["."])
            )
            record.tool_state = ToolTaskState(
                messages=loop_result.messages,
                boundary=previous_boundary,
                steps=len(task_invocations),
            )
        self.runtime_tasks[task_id] = record
        return record


def _task_context_payload(record: RuntimeTaskRecord) -> dict[str, Any]:
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
