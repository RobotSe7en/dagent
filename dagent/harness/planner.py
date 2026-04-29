"""Planner interfaces and a deterministic mock planner."""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import uuid4

from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode


class Planner(ABC):
    """Base planner interface.

    Planners propose DAGs. They do not grant final permissions.
    """

    @abstractmethod
    def plan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        """Create a proposed DAG for a user request."""


class MockPlanner(Planner):
    """Deterministic planner for tests and early development."""

    def plan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        return DAG(
            dag_id=f"dag_{uuid4().hex}",
            task_id=resolved_task_id,
            status="draft",
            nodes=[
                DAGNode(
                    id="understand_request",
                    title="Understand request",
                    goal=f"Analyze the user request: {user_request}",
                    tools=[],
                    boundary=Boundary(mode="read_only"),
                    risk="low",
                    risk_reason="Read-only planning step.",
                    expected_output="A concise interpretation of the request.",
                ),
                DAGNode(
                    id="produce_answer",
                    title="Produce answer",
                    goal="Produce a response based on the interpreted request.",
                    tools=[],
                    boundary=Boundary(mode="read_only"),
                    risk="low",
                    risk_reason="No tool or filesystem access requested.",
                    expected_output="Final answer for the user.",
                ),
            ],
            edges=[
                DAGEdge(
                    source="understand_request",
                    target="produce_answer",
                    reason="The answer depends on understanding the request.",
                )
            ],
        )
