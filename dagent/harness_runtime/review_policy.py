"""Review policy for human checkpoints during DAG execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dagent.schemas import DAG
from dagent.tools.registry import Tool


ReviewLevel = Literal["fast", "balanced", "careful", "manual"]
ReviewKind = Literal[
    "initial_dag",
    "arg_injection",
    "dag_replan",
    "execution_error",
    "node_execution",
    "boundary_change",
    "tool_execution",
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

    def requires_tool_review(self, tool_risk: str) -> bool:
        if self.level == "fast":
            return False
        if self.level == "balanced":
            return tool_risk == "high"
        if self.level == "careful":
            return tool_risk in {"medium", "high"}
        return True

    def requires_dag_revision_review(self, *, current: DAG, proposed: DAG) -> bool:
        if self.level == "manual":
            return True
        return self.level in {"balanced", "careful"} and _dag_revision_changes_execution(current, proposed)


def review_policy(level: ReviewLevel | None) -> ReviewPolicy:
    return ReviewPolicy(level=level or "balanced")


def effective_risk(tool: Tool | None, args: dict | None = None) -> str:
    """Compute the effective risk for a tool call.

    If the tool provides a risk_fn, it is called with the args to compute
    dynamic risk (e.g. run_command checks command whitelist).
    Otherwise, the static Tool.risk is returned.
    """
    if tool is None:
        return "low"
    if tool.risk_fn is not None and args is not None:
        return tool.risk_fn(args)
    return tool.risk


def _has_medium_or_high_risk(dag: DAG) -> bool:
    return any(node.risk in {"medium", "high"} for node in dag.nodes)


def _dag_revision_changes_execution(current: DAG, proposed: DAG) -> bool:
    current_nodes = {node.id: node for node in current.nodes}
    proposed_nodes = {node.id: node for node in proposed.nodes}
    if set(current_nodes) != set(proposed_nodes) or current.edges != proposed.edges:
        return True
    for node_id, proposed_node in proposed_nodes.items():
        current_node = current_nodes[node_id]
        if (
            current_node.tool != proposed_node.tool
            or current_node.args != proposed_node.args
            or current_node.boundary != proposed_node.boundary
        ):
            return True
    return False
