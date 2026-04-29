"""Planner interfaces, mock planner, and LLM-backed planner."""

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from typing import Any
from uuid import uuid4

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

    def __init__(self, provider: ChatProvider) -> None:
        self.provider = provider

    def plan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        return asyncio.run(self.aplan(user_request, task_id=task_id))

    async def aplan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        response = await self.provider.chat(
            [
                {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _planner_user_prompt(user_request, resolved_task_id),
                },
            ]
        )
        payload = _extract_json_object(response.content)
        payload.setdefault("dag_id", f"dag_{uuid4().hex}")
        payload["task_id"] = resolved_task_id
        payload.setdefault("version", 1)
        payload.setdefault("status", "draft")
        payload.setdefault("nodes", [])
        payload.setdefault("edges", [])
        return DAG.model_validate(payload)


_PLANNER_SYSTEM_PROMPT = """You are the DAG planner for dagent.

Return only one JSON object. Do not include markdown fences or explanation.

The JSON must match this shape:
{
  "dag_id": "dag_<short_id>",
  "task_id": "<provided task id>",
  "version": 1,
  "status": "draft",
  "nodes": [
    {
      "id": "snake_case_id",
      "title": "short title",
      "goal": "specific node goal",
      "agent": null,
      "tools": ["read_file"],
      "skills": [],
      "boundary": {
        "mode": "read_only",
        "allowed_paths": [],
        "forbidden_tools": [],
        "allowed_commands": [],
        "forbidden_commands": []
      },
      "risk": "low",
      "risk_reason": "why this risk is appropriate",
      "expected_output": "what this node should produce",
      "max_steps": 8,
      "timeout_seconds": 300
    }
  ],
  "edges": [
    {"source": "node_a", "target": "node_b", "reason": "dependency reason"}
  ]
}

Allowed tools are: read_file, grep, write_file.
Use no tools for pure reasoning. Use read_file/grep for repository inspection.
Use write_file only when the user asks to modify files.

Risk rules:
- read_file and grep are low risk unless the boundary is broad.
- write_file is at least medium risk.
- shell/delete/db/deploy/send_message are not available.
- allowed_paths ["."] or ["./"] is at least medium risk.

Keep DAGs small: 1-4 nodes unless the user request clearly needs more.
"""


def _planner_user_prompt(user_request: str, task_id: str) -> str:
    return (
        f"Task id: {task_id}\n"
        f"User request:\n{user_request}\n\n"
        "Generate the reviewable DAG JSON now."
    )


def _extract_json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("Planner response did not contain a JSON object.")
        parsed = json.loads(stripped[start : end + 1])

    if not isinstance(parsed, dict):
        raise ValueError("Planner response JSON must be an object.")
    return parsed
