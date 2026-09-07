"""Public review helpers for resumable dagent runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, StrictBool
from dagent.schemas.results import PendingCapabilityReviewItem

from dagent.schemas import DAG, PendingReview, ReviewKind


ReviewLevel = Literal["fast", "careful"]


@dataclass(frozen=True)
class _ReviewPolicy:
    level: ReviewLevel = "fast"

    def reviews_dag_changes(self) -> bool:
        return self.level == "careful"

    def reviews_tool(self, risk: str) -> bool:
        return self.level == "careful" and risk in {"medium", "high"}


def _review_policy(level: ReviewLevel | None) -> _ReviewPolicy:
    return _ReviewPolicy(level=level or "fast")


class CapabilityReviewDecision(BaseModel):
    """A decision for one invocation in a pending capability review."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    invocation_id: str
    approved: StrictBool


@dataclass(frozen=True)
class ReviewDecision:
    """A user's decision for a pending human review checkpoint."""

    review_id: str
    approved: bool | None = None
    dag: DAG | None = None
    review_level: ReviewLevel | None = None
    feedback: str | None = None
    capability_decisions: tuple[CapabilityReviewDecision, ...] = ()

    def __post_init__(self) -> None:
        if any(not isinstance(item, CapabilityReviewDecision) for item in self.capability_decisions):
            raise TypeError("capability_decisions must contain CapabilityReviewDecision objects.")
        object.__setattr__(self, "capability_decisions", tuple(self.capability_decisions))
        if (self.approved is not None) == bool(self.capability_decisions):
            raise ValueError("Provide either approved or capability_decisions, not both.")
        if self.approved is not None and not isinstance(self.approved, bool):
            raise TypeError("approved must be a boolean.")
        if self.capability_decisions and self.dag is not None:
            raise ValueError("Capability decisions cannot contain a DAG.")
        ids = [item.invocation_id for item in self.capability_decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("Capability decision invocation ids must be unique.")

    def validate_for(self, pending: PendingReview) -> None:
        """Validate before claiming a review or performing any runtime mutation."""
        if self.review_id != pending.review_id:
            raise ValueError("Decision review_id does not match the pending review.")
        if self.capability_decisions:
            if pending.kind != "capability_review":
                raise ValueError("Per-call decisions require a capability review.")
            expected = {item.invocation_id for item in pending.capability_items}
            if {item.invocation_id for item in self.capability_decisions} != expected:
                raise ValueError("Capability decisions must cover exactly the pending invocation ids.")

    def decisions_for(self, pending: PendingReview) -> dict[str, bool]:
        self.validate_for(pending)
        if self.capability_decisions:
            return {item.invocation_id: item.approved for item in self.capability_decisions}
        return {item.invocation_id: bool(self.approved) for item in pending.capability_items}



@dataclass(frozen=True)
class ReviewHandle:
    """Stable facade around an internal PendingReview."""

    pending: PendingReview

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
        if self.pending.capability_call is None:
            return None
        return self.pending.capability_call.model_dump(mode="json")

    @property
    def capability_calls(self) -> tuple[PendingCapabilityReviewItem, ...]:
        return self.pending.capability_items

    def decide(
        self,
        capability_decisions: Sequence[CapabilityReviewDecision],
        *,
        review_level: ReviewLevel | None = None,
        feedback: str | None = None,
    ) -> ReviewDecision:
        decision = ReviewDecision(
            review_id=self.review_id,
            capability_decisions=tuple(capability_decisions),
            review_level=review_level,
            feedback=feedback,
        )
        decision.validate_for(self.pending)
        return decision

    @property
    def payload(self) -> dict[str, Any]:
        return self.pending.payload

    def approve(
        self,
        *,
        dag: DAG | None = None,
        review_level: ReviewLevel | None = None,
        feedback: str | None = None,
    ) -> ReviewDecision:
        return ReviewDecision(
            review_id=self.review_id,
            approved=True,
            dag=dag or self.dag,
            review_level=review_level,
            feedback=feedback,
        )

    def reject(
        self,
        *,
        review_level: ReviewLevel | None = None,
        feedback: str | None = None,
    ) -> ReviewDecision:
        return ReviewDecision(
            review_id=self.review_id,
            approved=False,
            review_level=review_level,
            feedback=feedback,
        )


def _append_reviewer_feedback(message: str, feedback: str | None) -> str:
    text = (feedback or "").strip()
    if not text:
        return message
    return f"{message}\nReviewer feedback: {text}"
