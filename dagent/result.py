"""Public run result facade for the dagent SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dagent.review import ReviewHandle
from dagent.schemas import DAG, LoopStatus, PendingReview, RunTrace, RuntimeResponse


@dataclass(frozen=True)
class RunResult:
    """Stable public wrapper around RuntimeResponse."""

    raw_response: RuntimeResponse

    @property
    def raw(self) -> RuntimeResponse:
        return self.raw_response

    @property
    def status(self) -> LoopStatus:
        return self.raw_response.status

    @property
    def final_answer(self) -> str:
        return self.raw_response.final_answer

    @property
    def output_text(self) -> str:
        return self.raw_response.final_answer

    @property
    def output(self) -> str:
        return self.raw_response.final_answer

    @property
    def dag(self) -> DAG | None:
        return self.raw_response.dag

    @property
    def trace(self) -> RunTrace | None:
        return self.raw_response.trace

    @property
    def task_id(self) -> str | None:
        return self.raw_response.task_id

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.raw_response.events

    @property
    def pending_review(self) -> PendingReview | None:
        return self.raw_response.pending_review

    @property
    def requires_review(self) -> bool:
        return self.raw_response.status == "awaiting_review" and self.raw_response.pending_review is not None

    @property
    def awaiting_review(self) -> bool:
        return self.requires_review

    @property
    def review(self) -> ReviewHandle | None:
        if self.raw_response.pending_review is None:
            return None
        return ReviewHandle(self.raw_response.pending_review)
