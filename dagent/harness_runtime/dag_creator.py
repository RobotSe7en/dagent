"""DAG creator interfaces, mock creator, and LLM-backed creator."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from uuid import uuid4

from dagent.harness_runtime.profiled_agent import extract_json_object
from dagent.profiles import AgentProfile, ProfileStore
from dagent.providers import ChatProvider
from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode, PlanSpec
from dagent.state import PromptBuilder, PromptRequest
from dagent.tools.boundary import DEFAULT_READ_ONLY_COMMANDS
from dagent.tools.registry import Tool


class DagCreator(ABC):
    """Base DAG creator interface.

    DAG creators propose DAGs. They do not grant final permissions.
    """

    @abstractmethod
    def plan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        """Create a proposed DAG for a user request."""

    async def aplan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        return self.plan(user_request, task_id=task_id)


class MockDagCreator(DagCreator):
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


class LLMDagCreator(DagCreator):
    """DAG creator that asks an OpenAI-compatible model to produce DAG JSON."""

    def __init__(
        self,
        provider: ChatProvider,
        *,
        profile: AgentProfile | None = None,
        profile_store: ProfileStore | None = None,
        profile_name: str = "dag_creator",
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
                        "Generate the compact PlanSpec JSON now."
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
        if _is_full_dag_payload(payload):
            return _full_dag_from_payload(payload, resolved_task_id)

        plan = PlanSpec.model_validate(payload)
        return compile_plan_spec(plan, task_id=resolved_task_id)


def compile_plan_spec(plan: PlanSpec, *, task_id: str) -> DAG:
    return DAG(
        dag_id=f"dag_{uuid4().hex}",
        task_id=task_id,
        status="draft",
        nodes=[_compile_plan_node(node) for node in plan.nodes],
        edges=[
            DAGEdge(
                source=dependency,
                target=node.id,
                reason=f"{node.id} depends on {dependency}.",
            )
            for node in plan.nodes
            for dependency in node.depends_on
        ],
    )


def _compile_plan_node(node) -> DAGNode:
    tool = node.tool
    args = dict(node.args)
    boundary = _infer_boundary(tool, args)
    goal = node.goal
    if tool:
        goal = f"{goal}\nUse tool `{tool}` with arguments: {args}."

    return DAGNode(
        id=node.id,
        title=_title_from_id(node.id),
        goal=goal,
        tools=[tool] if tool else [],
        boundary=boundary,
        risk=node.risk or _infer_risk(tool, boundary),
        risk_reason=node.review_reason or _risk_reason(tool, boundary),
        expected_output=node.goal,
        max_steps=4 if tool else 2,
        timeout_seconds=120,
    )


def _infer_boundary(tool: str | None, args: dict) -> Boundary:
    if tool == "write_file":
        return Boundary(
            mode="write_limited",
            allowed_paths=[str(args.get("path") or ".")],
        )
    if tool == "run_command":
        command = str(args.get("command") or "").strip()
        executable = _command_executable(command)
        cwd = str(args.get("cwd") or ".")
        is_default_read_only = executable in DEFAULT_READ_ONLY_COMMANDS
        return Boundary(
            mode="read_only" if is_default_read_only else "write_limited",
            allowed_paths=[cwd],
            allowed_commands=[] if is_default_read_only else [executable or command],
        )
    if tool in {"read_file", "grep"}:
        return Boundary(
            mode="read_only",
            allowed_paths=[str(args.get("path") or ".")],
        )
    return Boundary(mode="read_only")


def _infer_risk(tool: str | None, boundary: Boundary) -> str:
    if tool == "write_file":
        return "medium"
    if tool == "run_command" and boundary.mode != "read_only":
        return "medium"
    return "low"


def _risk_reason(tool: str | None, boundary: Boundary) -> str:
    if tool:
        return f"PlanSpec inferred from tool={tool}, boundary={boundary.mode}."
    return "Pure reasoning node."


def _title_from_id(node_id: str) -> str:
    return node_id.replace("_", " ").strip().title() or "Node"


def _command_executable(command: str) -> str:
    return command.split(maxsplit=1)[0] if command else ""


def _is_full_dag_payload(payload: dict) -> bool:
    if payload.get("dag_id") or payload.get("task_id") or payload.get("edges"):
        return True
    nodes = payload.get("nodes", [])
    return any(
        isinstance(node, dict)
        and any(key in node for key in {"title", "tools", "boundary", "expected_output"})
        for node in nodes
    ) if isinstance(nodes, list) else False


def _full_dag_from_payload(payload: dict, task_id: str) -> DAG:
    payload.setdefault("dag_id", f"dag_{uuid4().hex}")
    payload["task_id"] = task_id
    payload.setdefault("version", 1)
    payload.setdefault("status", "draft")
    payload.setdefault("nodes", [])
    payload.setdefault("edges", [])
    _normalize_boundary_modes(payload)
    return DAG.model_validate(payload)


def _normalize_boundary_modes(payload: dict) -> None:
    aliases = {
        "readonly": "read_only",
        "read": "read_only",
        "read-only": "read_only",
        "write": "write_limited",
        "write_only": "write_limited",
        "write-only": "write_limited",
        "read_write": "write_limited",
        "read-write": "write_limited",
        "readwrite": "write_limited",
        "rw": "write_limited",
        "all": "full",
        "unrestricted": "full",
    }
    nodes = payload.get("nodes", [])
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        boundary = node.get("boundary")
        if not isinstance(boundary, dict):
            continue
        mode = boundary.get("mode")
        if not isinstance(mode, str):
            continue
        normalized = aliases.get(mode.strip().lower())
        if normalized:
            boundary["mode"] = normalized
