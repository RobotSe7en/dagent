"""DAG agent interfaces and LLM-backed agent."""

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


class DAGCreationError(ValueError):
    """Raised when a proposed DAG cannot become an executable tool DAG."""


class DAGAgent(ABC):
    """Base DAG agent interface.

    DAG agents propose DAGs. They do not grant final permissions.
    """

    tools: list[Tool] = []

    @abstractmethod
    def plan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        """Create a proposed DAG for a user request."""

    async def aplan(self, user_request: str, *, task_id: str | None = None) -> DAG:
        return self.plan(user_request, task_id=task_id)


class LLMDAGAgent(DAGAgent):
    """DAG agent that asks an OpenAI-compatible model to produce DAG JSON."""

    def __init__(
        self,
        provider: ChatProvider,
        *,
        profile: AgentProfile | None = None,
        profile_store: ProfileStore | None = None,
        profile_name: str = "dag_agent",
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
        return dag_from_json_payload(payload, task_id=resolved_task_id)


def dag_from_json_payload(payload: dict, *, task_id: str) -> DAG:
    if _is_full_dag_payload(payload):
        return _full_dag_from_payload(payload, task_id)

    plan = PlanSpec.model_validate(payload)
    return compile_plan_spec(plan, task_id=task_id)


def compile_plan_spec(plan: PlanSpec, *, task_id: str) -> DAG:
    nodes = [_compile_plan_node(node, task=plan.task) for node in plan.nodes]
    edges = [
        DAGEdge(
            source=dependency,
            target=node.id,
            reason=f"{node.id} depends on {dependency}.",
        )
        for node in plan.nodes
        for dependency in node.depends_on
    ]
    if len(nodes) > 1:
        nodes, edges = _ensure_start_node(nodes, edges)
    return DAG(
        dag_id=f"dag_{uuid4().hex}",
        task_id=task_id,
        status="draft",
        nodes=nodes,
        edges=edges,
    )


def _compile_plan_node(node, *, task: str = "") -> DAGNode:
    if not node.tool:
        raise DAGCreationError(
            f"PlanSpec node '{node.id}' must declare one concrete tool."
        )
    args = dict(node.args)
    boundary = _infer_boundary(node.tool, args)

    return DAGNode(
        id=node.id,
        tool=node.tool,
        args=args,
        boundary=boundary,
        risk=node.risk if node.risk in {"low", "medium", "high"} else _infer_risk(node.tool, boundary),
    )


def _ensure_start_node(
    nodes: list[DAGNode],
    edges: list[DAGEdge],
) -> tuple[list[DAGNode], list[DAGEdge]]:
    node_ids = {node.id for node in nodes}
    start_id = "start"
    next_nodes = list(nodes)
    next_edges = list(edges)
    if start_id not in node_ids:
        next_nodes = [
            DAGNode(
                id=start_id,
                tool="dag_start",
                args={},
                boundary=Boundary(mode="read_only"),
                risk="low",
            ),
            *next_nodes,
        ]
        node_ids.add(start_id)

    targets = {edge.target for edge in next_edges}
    existing_start_targets = {edge.target for edge in next_edges if edge.source == start_id}
    root_ids = [
        node.id
        for node in next_nodes
        if node.id != start_id and node.id not in targets and node.id not in existing_start_targets
    ]
    next_edges.extend(
        DAGEdge(
            source=start_id,
            target=node_id,
            reason=f"{node_id} starts after start.",
        )
        for node_id in root_ids
    )
    return next_nodes, next_edges


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


def _command_executable(command: str) -> str:
    return command.split(maxsplit=1)[0] if command else ""


def _is_full_dag_payload(payload: dict) -> bool:
    if payload.get("dag_id") or payload.get("task_id") or payload.get("edges"):
        return True
    nodes = payload.get("nodes", [])
    return any(
        isinstance(node, dict)
        and any(key in node for key in {"boundary"})
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
    _normalize_tool_nodes(payload)
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


def _normalize_tool_nodes(payload: dict) -> None:
    nodes = payload.get("nodes", [])
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        tool = node.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            tools = node.get("tools")
            if isinstance(tools, list) and len(tools) == 1 and isinstance(tools[0], str):
                node["tool"] = tools[0]
            else:
                node_id = str(node.get("id") or "<unknown>")
                raise DAGCreationError(
                    f"Full DAG node '{node_id}' must declare one concrete tool."
                )
        node.setdefault("args", {})


