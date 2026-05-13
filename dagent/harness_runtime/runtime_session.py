"""Session-scoped state helpers for the harness runtime."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

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
from dagent.schemas import Boundary, ToolExecutionRecord, ToolInvocation


MAX_CONVERSATION_HISTORY_MESSAGES = 20


class HarnessRuntimeSession:
    """Mutable conversation and review state owned by a harness session."""

    def __init__(
        self,
        *,
        initial_tasks: dict[str, RuntimeTaskRecord] | None = None,
    ) -> None:
        self.tasks: dict[str, RuntimeTaskRecord] = dict(initial_tasks or {})
        self.conversation_history: list[dict[str, Any]] = []
        self.runtime_tasks = self.tasks
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
    ) -> None:
        review = loop_result.pending_review
        if review is None:
            return
        self._review_continuations[review.review_id] = ReviewContinuation(
            review_id=review.review_id,
            task_id=task_id,
            kind=review.kind,
            user_request=user_request,
            messages=loop_result.messages,
            invocations=loop_result.invocations,
            review_level=review_level,
            pending_invocation=_pending_review_invocation(loop_result),
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

    def record_loop_outcome(
        self,
        *,
        task_id: str | None,
        mode: RuntimeTaskMode,
        user_request: str,
        review_level: ReviewLevel,
        needs_human_review: bool,
        loop_result: LoopResult,
        invocations: list[ToolInvocation] | None = None,
    ) -> RuntimeTaskRecord:
        resolved_task_id = task_id or loop_result.task_id or f"task_{uuid4().hex}"
        record = self.record_runtime_task(
            task_id=resolved_task_id,
            mode=mode,
            user_request=user_request,
            review_level=review_level,
            status=(
                "awaiting_review"
                if needs_human_review
                else "completed" if loop_result.completed else "failed"
            ),
            loop_result=loop_result,
            invocations=invocations,
        )
        if needs_human_review:
            self.store_review_continuation(
                task_id=record.task_id,
                user_request=user_request,
                review_level=review_level,
                loop_result=loop_result,
            )
        return record

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
        execution_records = list(existing.execution_records) if existing else []
        if loop_result.run_result is not None:
            execution_records = list(loop_result.run_result.execution_records)
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
        if loop_result.dag is not None:
            if record.dag_state is None:
                record.dag_state = DAGTaskState(dag=loop_result.dag)
            record.dag_state.dag = loop_result.dag
            if loop_result.run_result and loop_result.run_result not in record.dag_state.runs:
                record.dag_state.runs.append(loop_result.run_result)
            if loop_result.run_result:
                record.dag_state.node_results = loop_result.run_result.node_results
            record.execution_records = execution_records
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
            record.execution_records = _tool_loop_execution_records(
                task_id=task_id,
                messages=loop_result.messages,
                invocations=task_invocations,
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
        "recent_execution_records": [
            {
                "node_id": record_item.node_id,
                "tool": record_item.invocation.tool_name,
                "status": record_item.status,
                "output": record_item.output,
                "error": record_item.error,
            }
            for record_item in record.execution_records[-8:]
        ],
    }


def _pending_review_invocation(loop_result: LoopResult) -> ToolInvocation | None:
    review = loop_result.pending_review
    if review is None or review.kind != "tool_review":
        return None
    tool_call_id = (review.tool_call or {}).get("tool_call_id")
    if tool_call_id:
        for invocation in reversed(loop_result.invocations):
            if invocation.invocation_id == tool_call_id:
                return invocation
    return loop_result.invocations[-1] if loop_result.invocations else None


def _tool_loop_execution_records(
    *,
    task_id: str,
    messages: list[dict[str, Any]],
    invocations: list[ToolInvocation],
) -> list[ToolExecutionRecord]:
    invocations_by_id = {
        invocation.invocation_id: invocation
        for invocation in invocations
    }
    tool_messages: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        tool_call_id = message.get("tool_call_id")
        if not isinstance(tool_call_id, str) or tool_call_id not in invocations_by_id:
            continue
        content = str(message.get("content", ""))
        if content.startswith("[PENDING_REVIEW]") or content.startswith("[DENIED]"):
            continue
        tool_messages[tool_call_id] = message

    records: list[ToolExecutionRecord] = []
    for tool_call_id, message in tool_messages.items():
        content = str(message.get("content", ""))
        failed = content.startswith(("[TOOL_ERROR]", "[BOUNDARY_VIOLATION]", "[ERROR]"))
        records.append(
            ToolExecutionRecord(
                record_id=f"tool_execution_{uuid4().hex}",
                task_id=task_id,
                invocation=invocations_by_id[tool_call_id].model_copy(deep=True),
                source="tool_loop",
                output="" if failed else content,
                error=content if failed else None,
                status="failed" if failed else "completed",
                stop_reason="tool_error" if failed else "completed",
                steps=1,
            )
        )
    return records


def _conversation_message_copy(message: dict[str, Any]) -> dict[str, Any] | None:
    role = message.get("role")
    if role not in {"user", "assistant"}:
        return None
    content = message.get("content")
    if not content:
        return None
    return {"role": role, "content": content}
