"""Review policy for human checkpoints during DAG execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dagent.tools.registry import Tool


ReviewLevel = Literal["fast", "careful"]
ReviewKind = Literal["initial_dag", "dag_replan"]


@dataclass(frozen=True)
class ReviewPolicy:
    level: ReviewLevel = "fast"

    def reviews_dag_changes(self) -> bool:
        return self.level == "careful"


def review_policy(level: ReviewLevel | None) -> ReviewPolicy:
    return ReviewPolicy(level=level or "fast")


def effective_risk(tool: Tool | None, args: dict | None = None) -> str:
    if tool is None:
        return "low"
    if tool.risk_fn is not None and args is not None:
        return tool.risk_fn(args)
    return tool.risk


