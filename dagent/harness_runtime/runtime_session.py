"""Session-scoped state helpers for the harness runtime."""

from __future__ import annotations

from uuid import uuid4

from dagent.harness_runtime.review_policy import ReviewLevel
from dagent.harness_runtime.task_record import (
    ReviewContinuation,
    RuntimeTaskMode,
    RuntimeTaskRecord,
    pending_review_invocation,
)
from dagent.schemas import LoopOutcome, CapabilityInvocation


class HarnessRuntimeSession:
    """Mutable task and review state owned by a harness session."""

    def __init__(self) -> None:
        self.tasks: dict[str, RuntimeTaskRecord] = {}
        self._review_continuations: dict[str, ReviewContinuation] = {}

    def store_review_continuation(
        self,
        *,
        task_id: str,
        user_request: str,
        review_level: ReviewLevel,
        loop_outcome: LoopOutcome,
        invocations: list[CapabilityInvocation] | None = None,
    ) -> None:
        review = loop_outcome.pending_review
        if review is None:
            return
        task_invocations = invocations if invocations is not None else loop_outcome.invocations
        self._review_continuations[review.review_id] = ReviewContinuation(
            review_id=review.review_id,
            task_id=task_id,
            kind=review.kind,
            user_request=user_request,
            invocations=task_invocations,
            review_level=review_level,
            pending_invocation=pending_review_invocation(loop_outcome, task_invocations),
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

    def save_loop_outcome(
        self,
        *,
        task_id: str | None,
        mode: RuntimeTaskMode,
        user_request: str,
        review_level: ReviewLevel,
        loop_outcome: LoopOutcome,
        invocations: list[CapabilityInvocation] | None = None,
        runtime_mode: str | None = None,
    ) -> RuntimeTaskRecord:
        resolved_task_id = task_id or loop_outcome.task_id or f"task_{uuid4().hex}"
        record = self.tasks.get(resolved_task_id)
        if record is None:
            if mode == "dag" and loop_outcome.dag is not None:
                record = RuntimeTaskRecord.dag_task(
                    task_id=resolved_task_id,
                    user_request=user_request,
                    dag=loop_outcome.dag,
                    review_level=review_level,
                    runtime_mode=runtime_mode or "auto",
                    spec_id=loop_outcome.spec_id,
                    workspace_path=loop_outcome.workspace_path,
                )
            else:
                if mode == "tool":
                    record = RuntimeTaskRecord.tool_task(
                        task_id=resolved_task_id,
                        user_request=user_request,
                        review_level=review_level,
                    )
                else:
                    record = RuntimeTaskRecord(
                        task_id=resolved_task_id,
                        mode=mode,
                        user_request=user_request,
                        review_level=review_level,
                        runtime_mode=runtime_mode or "auto",
                    )
        record.apply_outcome(
            loop_outcome,
            review_level=review_level,
        )
        self.tasks[resolved_task_id] = record
        if loop_outcome.status == "awaiting_review":
            self.store_review_continuation(
                task_id=record.task_id,
                user_request=user_request,
                review_level=review_level,
                loop_outcome=loop_outcome,
                invocations=invocations,
            )
        return record
