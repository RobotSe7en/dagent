"""Planner interfaces, mock planner, and LLM-backed planner."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from uuid import uuid4

from dagent.harness.profiled_agent import extract_json_object
from dagent.profiles import AgentProfile, ProfileStore
from dagent.providers import ChatProvider
from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode


class Planner(ABC):
    """Base planner interface.

    Planners propose DAGs. They do not grant final permissions.
    """

    @abstractmethod
    def plan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        """Create a proposed DAG for a user request."""

    async def aplan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        return self.plan(user_request, task_id=task_id)


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


class LLMPlanner(Planner):
    """Planner that asks an OpenAI-compatible model to produce a DAG JSON object."""

    def __init__(
        self,
        provider: ChatProvider,
        *,
        profile: AgentProfile | None = None,
        profile_store: ProfileStore | None = None,
        profile_name: str = "planner",
    ) -> None:
        self.provider = provider
        self.profile = profile or (profile_store or ProfileStore()).load(profile_name)

    def plan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        return asyncio.run(self.aplan(user_request, task_id=task_id))

    async def aplan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        response = await self.provider.chat(
            [
                {"role": "system", "content": self.profile.system_prompt},
                {
                    "role": "user",
                    "content": self.profile.render_user_prompt(
                        user_request=user_request,
                        task_id=resolved_task_id,
                    ),
                },
            ]
        )
        payload = extract_json_object(response.content)
        payload.setdefault("dag_id", f"dag_{uuid4().hex}")
        payload["task_id"] = resolved_task_id
        payload.setdefault("version", 1)
        payload.setdefault("status", "draft")
        payload.setdefault("nodes", [])
        payload.setdefault("edges", [])
        return DAG.model_validate(payload)
