"""DAG agent interfaces and LLM-backed agent."""

from __future__ import annotations

import asyncio
import ast
import re
from abc import ABC, abstractmethod
from typing import Any
from uuid import uuid4

from dagent.harness_runtime.profiled_agent import extract_json_object
from dagent.profiles import AgentProfile, ProfileStore
from dagent.providers import ChatProvider
from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode, PlanSpec
from dagent.state import PromptBuilder, PromptRequest
from dagent.harness_runtime.review_policy import effective_risk
from dagent.tools.registry import Tool


class DAGCreationError(ValueError):
    """Raised when a proposed DAG cannot become an executable tool DAG."""


_PLAN_NODE_RE = re.compile(
    r"^(?P<id>[A-Za-z][A-Za-z0-9_-]*)\s*=\s*"
    r"(?P<tool>[A-Za-z_][A-Za-z0-9_]*)\((?P<args>.*)\)"
    r"(?:\s+after\s+(?P<deps>.+))?$"
)


class DAGAgent(ABC):
    """Base DAG agent interface.

    DAG agents propose DAGs. They do not grant final permissions.
    """

    tools: list[Tool] = []

    @abstractmethod
    def plan(
        self,
        user_request: str,
        *,
        task_id: str | None = None,
        conversation_messages: list[dict[str, Any]] | None = None,
    ) -> DAG:
        """Create a proposed DAG for a user request."""

    async def aplan(
        self,
        user_request: str,
        *,
        task_id: str | None = None,
        conversation_messages: list[dict[str, Any]] | None = None,
    ) -> DAG:
        return self.plan(
            user_request,
            task_id=task_id,
            conversation_messages=conversation_messages,
        )


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

    def plan(
        self,
        user_request: str,
        *,
        task_id: str | None = None,
        conversation_messages: list[dict[str, Any]] | None = None,
    ) -> DAG:
        return asyncio.run(
            self.aplan(
                user_request,
                task_id=task_id,
                conversation_messages=conversation_messages,
            )
        )

    async def aplan(
        self,
        user_request: str,
        *,
        task_id: str | None = None,
        conversation_messages: list[dict[str, Any]] | None = None,
    ) -> DAG:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        messages = self.prompt_builder.build(
            PromptRequest(
                profile=self.profile,
                task_content=(
                    "Task id: {{ task_id }}\n"
                    "User request:\n{{ user_request }}\n\n"
                    "Generate the PlanSpec DSL now."
                ),
                tools=self.tools,
                memory=self.profile.memory,
                variables={
                    "user_request": user_request,
                    "task_id": resolved_task_id,
                },
            )
        )
        if conversation_messages:
            messages = [
                messages[0],
                *_conversation_message_copies(conversation_messages),
                messages[1],
            ]
        response = await self.provider.chat(
            messages
        )
        return dag_from_model_output(response.content, task_id=resolved_task_id, tools=self.tools)


def _conversation_message_copies(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    copied = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            copied.append({"role": role, "content": content})
    return copied


def dag_from_model_output(
    content: str,
    *,
    task_id: str,
    tools: list[Tool] | None = None,
) -> DAG:
    try:
        plan = parse_plan_spec_dsl(content)
    except DAGCreationError as dsl_error:
        try:
            payload = extract_json_object(content)
        except Exception as json_error:
            raise DAGCreationError(
                f"Agent response was neither valid PlanSpec DSL nor JSON. "
                f"DSL error: {dsl_error}; JSON error: {json_error}"
            ) from dsl_error
        return dag_from_json_payload(payload, task_id=task_id, tools=tools)
    return compile_plan_spec(plan, task_id=task_id, tools=tools)


def parse_plan_spec_dsl(content: str) -> PlanSpec:
    lines = _plan_spec_lines(content)
    task = ""
    nodes = []
    for line_number, line in lines:
        if line.lower().startswith("task:"):
            task = line.split(":", 1)[1].strip()
            continue
        match = _PLAN_NODE_RE.match(line)
        if not match:
            raise DAGCreationError(f"PlanSpec DSL line {line_number} is invalid: {line}")
        node_id = match.group("id")
        nodes.append(
            {
                "id": node_id,
                "tool": match.group("tool"),
                "args": _parse_args(match.group("args"), line_number=line_number),
                "depends_on": _parse_depends_on(match.group("deps")),
            }
        )

    if not nodes:
        raise DAGCreationError("PlanSpec DSL must contain at least one node line.")
    return PlanSpec.model_validate({"task": task, "nodes": nodes})


def _plan_spec_lines(content: str) -> list[tuple[int, str]]:
    stripped = _strip_thinking_blocks(content.strip())
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:planspec|text)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    if "PLAN_SPEC" in stripped:
        _, stripped = stripped.split("PLAN_SPEC", 1)
    if "END_PLAN_SPEC" in stripped:
        stripped, _ = stripped.split("END_PLAN_SPEC", 1)

    lines = []
    for line_number, raw_line in enumerate(stripped.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.lower().startswith("task:") and _PLAN_NODE_RE.match(line) is None:
            continue
        lines.append((line_number, line))
    return lines


def _strip_thinking_blocks(content: str) -> str:
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"<think>.*", "", content, flags=re.IGNORECASE | re.DOTALL)


def _parse_args(args_text: str, *, line_number: int) -> dict:
    args_text = args_text.strip()
    if not args_text:
        return {}
    try:
        expr = ast.parse(f"_tool({args_text})", mode="eval").body
    except SyntaxError as exc:
        raise DAGCreationError(f"PlanSpec DSL line {line_number} has invalid args syntax.") from exc
    if not isinstance(expr, ast.Call):
        raise DAGCreationError(f"PlanSpec DSL line {line_number} has invalid args syntax.")
    if len(expr.args) == 1 and isinstance(expr.args[0], ast.Dict) and not expr.keywords:
        try:
            value = ast.literal_eval(expr.args[0])
        except (ValueError, TypeError) as exc:
            raise DAGCreationError(f"PlanSpec DSL line {line_number} has invalid args value.") from exc
        if not isinstance(value, dict):
            raise DAGCreationError(f"PlanSpec DSL line {line_number} args must be an object.")
        return value
    if expr.args:
        raise DAGCreationError(f"PlanSpec DSL line {line_number} only supports keyword args.")
    parsed = {}
    for keyword in expr.keywords:
        if keyword.arg is None:
            raise DAGCreationError(f"PlanSpec DSL line {line_number} does not support **kwargs.")
        try:
            parsed[keyword.arg] = ast.literal_eval(keyword.value)
        except (ValueError, TypeError) as exc:
            raise DAGCreationError(f"PlanSpec DSL line {line_number} has invalid args value.") from exc
    return parsed


def _parse_depends_on(deps_text: str | None) -> list[str]:
    if not deps_text:
        return []
    return [
        item.strip()
        for item in deps_text.split(",")
        if item.strip()
    ]


def dag_from_json_payload(
    payload: dict,
    *,
    task_id: str,
    tools: list[Tool] | None = None,
) -> DAG:
    if _is_full_dag_payload(payload):
        return _full_dag_from_payload(payload, task_id, tools=tools)

    plan = PlanSpec.model_validate(payload)
    return compile_plan_spec(plan, task_id=task_id, tools=tools)


def compile_plan_spec(
    plan: PlanSpec,
    *,
    task_id: str,
    tools: list[Tool] | None = None,
) -> DAG:
    tool_index = {t.name: t for t in (tools or [])}
    nodes = [_compile_plan_node(node, task=plan.task, tool_index=tool_index) for node in plan.nodes]
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


def _compile_plan_node(
    node,
    *,
    task: str = "",
    tool_index: dict[str, Tool] | None = None,
) -> DAGNode:
    if not node.tool:
        raise DAGCreationError(
            f"PlanSpec node '{node.id}' must declare one concrete tool."
        )
    args = dict(node.args)
    registered = (tool_index or {}).get(node.tool)
    boundary = _infer_boundary(registered, args)

    risk = effective_risk(registered, args)

    return DAGNode(
        id=node.id,
        tool=node.tool,
        args=args,
        boundary=boundary,
        risk=risk,
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


def _infer_boundary(tool_obj: Tool | None, args: dict) -> Boundary:
    """Infer boundary from tool registration info.

    If the tool provides a custom boundary_fn, use it.
    Otherwise, derive boundary from Tool.action and Tool.path_args.
    """
    if tool_obj is not None and tool_obj.boundary_fn is not None:
        return tool_obj.boundary_fn(args)
    if tool_obj is None:
        return Boundary(mode="read_only")
    paths = [str(args.get(p) or ".") for p in tool_obj.path_args] or ["."]
    if tool_obj.action == "write":
        return Boundary(mode="write_limited", allowed_paths=paths)
    return Boundary(mode="read_only", allowed_paths=paths)





def _is_full_dag_payload(payload: dict) -> bool:
    if payload.get("dag_id") or payload.get("task_id") or payload.get("edges"):
        return True
    nodes = payload.get("nodes", [])
    return any(
        isinstance(node, dict)
        and any(key in node for key in {"boundary"})
        for node in nodes
    ) if isinstance(nodes, list) else False


def _full_dag_from_payload(
    payload: dict,
    task_id: str,
    *,
    tools: list[Tool] | None = None,
) -> DAG:
    payload.setdefault("dag_id", f"dag_{uuid4().hex}")
    payload["task_id"] = task_id
    payload.setdefault("version", 1)
    payload.setdefault("status", "draft")
    payload.setdefault("nodes", [])
    payload.setdefault("edges", [])
    _normalize_boundary_modes(payload)
    _normalize_tool_nodes(payload)
    dag = DAG.model_validate(payload)
    _apply_effective_risk(dag, tools)
    return dag


def _apply_effective_risk(dag: DAG, tools: list[Tool] | None = None) -> None:
    """Recompute risk for every node using effective_risk(tool, args)."""
    tool_index = {t.name: t for t in (tools or [])}
    for node in dag.nodes:
        registered = tool_index.get(node.tool) if node.tool else None
        node.risk = effective_risk(registered, node.args)


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


