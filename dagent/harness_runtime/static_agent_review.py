"""Internal bridge between static DAG nodes and tool-agent review state."""

from __future__ import annotations

from dataclasses import dataclass, replace

from dagent.review import ReviewLevel
from dagent.schemas import CapabilityInvocation, RunState, RunTrace


@dataclass(frozen=True)
class StaticAgentExecutionControl:
    """Execution-only data supplied to one direct static-DAG agent node."""

    review_level: ReviewLevel
    agent_state: RunState | None = None
    node_id: str | None = None
    approved: bool = True
    feedback: str | None = None

    def for_node(self, *, resume: bool) -> "StaticAgentExecutionControl":
        """Return control for a node, retaining state only for its resume."""
        return self if resume else replace(self, agent_state=None, feedback=None)


class StaticAgentReviewRequired(RuntimeError):
    """Private suspension signal raised when an agent tool call needs review."""

    def __init__(self, state: RunState) -> None:
        self.agent_state = state
        self.node_id: str | None = None
        self.invocation: CapabilityInvocation | None = None
        self.trace: RunTrace | None = None
        super().__init__("Static DAG agent node is awaiting tool review.")

    def bind(
        self,
        *,
        node_id: str | None = None,
        invocation: CapabilityInvocation | None = None,
        trace: RunTrace | None = None,
    ) -> "StaticAgentReviewRequired":
        if node_id is not None:
            self.node_id = node_id
        if invocation is not None:
            self.invocation = invocation.model_copy(deep=True)
        if trace is not None:
            self.trace = trace.model_copy(deep=True)
        return self
