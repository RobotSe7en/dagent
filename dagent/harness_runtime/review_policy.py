"""Review policy for human checkpoints during DAG execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dagent.harness_runtime.dag_replanner import ReplanDecision
from dagent.schemas import DAG


ReviewLevel = Literal["fast", "balanced", "careful", "manual"]
ReviewKind = Literal[
    "initial_dag",
    "arg_injection",
    "node_patch",
    "dag_replan",
    "execution_error",
    "node_execution",
    "boundary_change",
]


@dataclass(frozen=True)
class ReviewPolicy:
    level: ReviewLevel = "balanced"

    def requires_initial_dag_review(self, dag: DAG) -> bool:
        if self.level == "fast":
            return _has_medium_or_high_risk(dag)
        return True

    def requires_arg_injection_review(self) -> bool:
        return self.level in {"careful", "manual"}

    def requires_node_execution_review(self) -> bool:
        return self.level == "manual"

    def requires_replan_review(self, decision: ReplanDecision, *, current: DAG) -> bool:
        if decision.action == "abort":
            return False
        if self.level == "manual":
            return True
        if decision.action == "replace":
            return self.level in {"balanced", "careful"}
        if decision.action == "patch_node":
            if self.level == "careful":
                return True
            if self.level == "balanced":
                return _patch_changes_tool_or_boundary(decision, current=current)
        return False


def review_policy(level: ReviewLevel | None) -> ReviewPolicy:
    return ReviewPolicy(level=level or "balanced")


def _has_medium_or_high_risk(dag: DAG) -> bool:
    return any(node.risk in {"medium", "high"} for node in dag.nodes)


def _patch_changes_tool_or_boundary(decision: ReplanDecision, *, current: DAG) -> bool:
    if decision.boundary is not None:
        return True
    if decision.tool is None:
        return False
    for node in current.nodes:
        if node.id == decision.node_id:
            return node.tool != decision.tool
    return True
