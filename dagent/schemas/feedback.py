"""Feedback schema."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


FeedbackRating = Literal["positive", "negative", "neutral"]


class Feedback(BaseModel):
    feedback_id: str
    task_id: str | None = None
    dag_id: str | None = None
    run_id: str | None = None
    rating: FeedbackRating = "neutral"
    comment: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

