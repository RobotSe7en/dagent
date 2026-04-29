"""DAG creator interfaces, mock creator, and LLM-backed creator."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from uuid import uuid4

from dagent.harness_runtime.profiled_agent import extract_json_object
from dagent.profiles import AgentProfile, ProfileStore
from dagent.providers import ChatProvider
from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode
from dagent.state import PromptBuilder, PromptRequest
from dagent.tools.registry import Tool


class Planner(ABC):
    """Base DAG creator interface.

    DAG creators propose DAGs. They do not grant final permissions.
    """

    @abstractmethod
    def plan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        """Create a proposed DAG for a user request."""

    async def aplan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        return self.plan(user_request, task_id=task_id)


class MockPlanner(Planner):
    """Deterministic DAG creator for tests and early development."""

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
    """DAG creator that asks an OpenAI-compatible model to produce DAG JSON."""

    def __init__(
        self,
        provider: ChatProvider,
        *,
        profile: AgentProfile | None = None,
        profile_store: ProfileStore | None = None,
        profile_name: str = "planner",
        prompt_builder: PromptBuilder | None = None,
        tools: list[Tool] | None = None,
    ) -> None:
        self.provider = provider
        self.profile = profile or (profile_store or ProfileStore()).load(profile_name)
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.tools = tools or []

    def plan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        return asyncio.run(self.aplan(user_request, task_id=task_id))

    async def aplan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        response = await self.provider.chat(
            self.prompt_builder.build(
                PromptRequest(
                    profile=self.profile,
                    task_content=(
                        "Task id: {{ task_id }}\n"
                        "User request:\n{{ user_request }}\n\n"
                        "Generate the reviewable DAG JSON now."
                    ),
                    tools=self.tools,
                    memory=self.profile.memory,
                    variables={
                        "user_request": user_request,
                        "task_id": resolved_task_id,
                    },
                )
            )
        )
        payload = extract_json_object(response.content)
        payload.setdefault("dag_id", f"dag_{uuid4().hex}")
        payload["task_id"] = resolved_task_id
        payload.setdefault("version", 1)
        payload.setdefault("status", "draft")
        payload.setdefault("nodes", [])
        payload.setdefault("edges", [])
        return DAG.model_validate(payload)
