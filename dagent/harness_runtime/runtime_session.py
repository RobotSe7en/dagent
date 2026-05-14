"""Session-scoped state helpers for the harness runtime."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from dagent.harness_runtime.review_policy import ReviewLevel
from dagent.harness_runtime.task_record import (
    ReviewContinuation,
    RuntimeTaskMode,
    RuntimeTaskRecord,
    pending_review_invocation,
    task_context_payload,
)
from dagent.schemas import LoopOutcome, ToolInvocation


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
                task_context_payload(record)
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

    def record_conversation(self, loop_outcome: LoopOutcome) -> None:
        """Record loop messages into conversation history.

        For tool mode the full ToolAgent conversation replaces history.
        For DAG mode there are no conversation messages (DAG has internal state).
        """
        if loop_outcome.messages:
            self.remember_conversation_messages(loop_outcome.messages)

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
        loop_outcome: LoopOutcome,
    ) -> None:
        review = loop_outcome.pending_review
        if review is None:
            return
        self._review_continuations[review.review_id] = ReviewContinuation(
            review_id=review.review_id,
            task_id=task_id,
            kind=review.kind,
            user_request=user_request,
            messages=loop_outcome.messages,
            invocations=loop_outcome.invocations,
            review_level=review_level,
            pending_invocation=pending_review_invocation(loop_outcome),
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

    def record_outcome(
        self,
        *,
        task_id: str | None,
        mode: RuntimeTaskMode,
        user_request: str,
        review_level: ReviewLevel,
        loop_outcome: LoopOutcome,
        invocations: list[ToolInvocation] | None = None,
    ) -> RuntimeTaskRecord:
        resolved_task_id = task_id or loop_outcome.task_id or f"task_{uuid4().hex}"
        record = self.runtime_tasks.get(resolved_task_id)
        if record is None:
            if mode == "dag" and loop_outcome.dag is not None:
                record = RuntimeTaskRecord.dag_task(
                    task_id=resolved_task_id,
                    user_request=user_request,
                    dag=loop_outcome.dag,
                    review_level=review_level,
                )
            else:
                record = RuntimeTaskRecord(
                    task_id=resolved_task_id,
                    mode=mode,
                    user_request=user_request,
                    review_level=review_level,
                )
        record.apply_outcome(
            loop_outcome,
            review_level=review_level,
            invocations=invocations,
        )
        self.runtime_tasks[resolved_task_id] = record
        if loop_outcome.status == "awaiting_review":
            self.store_review_continuation(
                task_id=record.task_id,
                user_request=user_request,
                review_level=review_level,
                loop_outcome=loop_outcome,
            )
        return record


def _conversation_message_copy(message: dict[str, Any]) -> dict[str, Any] | None:
    role = message.get("role")
    if role not in {"user", "assistant"}:
        return None
    content = message.get("content")
    if not content:
        return None
    return {"role": role, "content": content}
