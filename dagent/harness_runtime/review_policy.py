"""Review policy for human checkpoints during DAG execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ReviewLevel = Literal["fast", "careful"]


@dataclass(frozen=True)
class ReviewPolicy:
    level: ReviewLevel = "fast"

    def reviews_dag_changes(self) -> bool:
        return self.level == "careful"

    def reviews_tool(self, risk: str) -> bool:
        return self.level == "careful" and risk in {"medium", "high"}


def review_policy(level: ReviewLevel | None) -> ReviewPolicy:
    return ReviewPolicy(level=level or "fast")

