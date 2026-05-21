"""Public review helpers for resumable dagent runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dagent.harness_runtime.review_policy import ReviewLevel
from dagent.schemas import DAG, PendingReview, ReviewKind


@dataclass(frozen=True)
class ReviewDecision:
    """A user's decision for a pending human review checkpoint."""

    review_id: str
    approved: bool
    dag: DAG | None = None
    review_level: ReviewLevel | None = None


@dataclass(frozen=True)
class ReviewHandle:
    """Stable facade around an internal PendingReview."""

    pending: PendingReview

    @property
    def id(self) -> str:
        return self.pending.review_id

    @property
    def review_id(self) -> str:
        return self.pending.review_id

    @property
    def kind(self) -> ReviewKind:
        return self.pending.kind

    @property
    def message(self) -> str:
        return self.pending.message

    @property
    def dag(self) -> DAG | None:
        return self.pending.proposed_dag

    @property
    def capability_call(self) -> dict[str, Any] | None:
        return self.pending.capability_call

    @property
    def payload(self) -> dict[str, Any]:
        return self.pending.payload

    def approve(
        self,
        *,
        dag: DAG | None = None,
        review_level: ReviewLevel | None = None,
    ) -> ReviewDecision:
        return ReviewDecision(
            review_id=self.id,
            approved=True,
            dag=dag or self.dag,
            review_level=review_level,
        )

    def reject(
        self,
        *,
        review_level: ReviewLevel | None = None,
    ) -> ReviewDecision:
        return ReviewDecision(
            review_id=self.id,
            approved=False,
            review_level=review_level,
        )
