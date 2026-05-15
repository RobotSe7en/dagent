"""Build executable DAGs from model output and validate DAG structure."""

from __future__ import annotations

from collections import defaultdict, deque
import ast
import re
from typing import Any
from uuid import uuid4

from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode, PlanSpec, CapabilityDefinition, CapabilityInvocation


class DAGCreationError(ValueError):
    """Raised when a proposed DAG cannot become an executable DAG."""


class DAGValidationError(ValueError):
    """Raised when a DAG violates structural validation rules."""


_PLAN_NODE_RE = re.compile(
    r"^(?P<id>[A-Za-z][A-Za-z0-9_-]*)\s*=\s*"
    r"(?P<tool>[A-Za-z_][A-Za-z0-9_]*)\((?P<args>.*)\)"
    r"(?:\s+after\s+(?P<deps>.+))?$"
)


def dag_from_model_output(
    content: str,
    *,
    task_id: str,
    tools: list[CapabilityDefinition] | None = None,
) -> DAG | str | None:
    """Parse LLM output into a DAG, a final answer string, or None."""
    if _is_no_change(content):
        return None
    try:
        plan = parse_plan_spec_dsl(content)
    except DAGCreationError:
        cleaned = strip_thinking_blocks(content).strip()
        return cleaned or None
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
        nodes.append(
            {
                "id": match.group("id"),
                "tool": match.group("tool"),
                "args": _parse_args(match.group("args"), line_number=line_number),
                "depends_on": _parse_depends_on(match.group("deps")),
            }
        )

    if not nodes:
        raise DAGCreationError("PlanSpec DSL must contain at least one node line.")
    return PlanSpec.model_validate({"task": task, "nodes": nodes})


def compile_plan_spec(
    plan: PlanSpec,
    *,
    task_id: str,
    tools: list[CapabilityDefinition] | None = None,
) -> DAG:
    tool_index = {tool.name: tool for tool in (tools or [])}
    nodes = [_compile_plan_node(node, tool_index=tool_index) for node in plan.nodes]
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


def validate_dag(dag: DAG) -> None:
    """Validate DAG structure."""
    if not dag.nodes:
        raise DAGValidationError("DAG must contain at least one node.")

    node_ids = [node.id for node in dag.nodes]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for node_id in node_ids:
        if node_id in seen:
            duplicates.add(node_id)
        seen.add(node_id)
    if duplicates:
        duplicate_list = ", ".join(sorted(duplicates))
        raise DAGValidationError(f"Duplicate node IDs: {duplicate_list}.")

    for node in dag.nodes:
        if node.node_type == "start":
            if node.id != "start":
                raise DAGValidationError("Start node must use id 'start'.")
            continue
        if not node.invocation.capability_id:
            raise DAGValidationError(f"Node '{node.id}' must declare a capability.")

    node_id_set = set(node_ids)
    connected_ids: set[str] = set()
    for edge in dag.edges:
        if edge.source not in node_id_set:
            raise DAGValidationError(
                f"Edge source '{edge.source}' does not reference an existing node."
            )
        if edge.target not in node_id_set:
            raise DAGValidationError(
                f"Edge target '{edge.target}' does not reference an existing node."
            )
        connected_ids.add(edge.source)
        connected_ids.add(edge.target)

    if len(node_id_set) > 1:
        isolated_ids = node_id_set - connected_ids
        if isolated_ids:
            isolated_list = ", ".join(sorted(isolated_ids))
            raise DAGValidationError(f"Isolated node IDs: {isolated_list}.")

    _ensure_acyclic(node_id_set, [(edge.source, edge.target) for edge in dag.edges])


def strip_thinking_blocks(content: str) -> str:
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"<think>.*", "", content, flags=re.IGNORECASE | re.DOTALL)


def _is_no_change(content: str) -> bool:
    cleaned = strip_thinking_blocks(content).strip().upper()
    return cleaned in {"NO_CHANGE", "NO CHANGE", "NOCHANGE"}


def _plan_spec_lines(content: str) -> list[tuple[int, str]]:
    stripped = strip_thinking_blocks(content.strip())
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


def _parse_args(args_text: str, *, line_number: int) -> dict[str, Any]:
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
    return [item.strip() for item in deps_text.split(",") if item.strip()]


def _compile_plan_node(
    node,
    *,
    tool_index: dict[str, CapabilityDefinition] | None = None,
) -> DAGNode:
    if not node.tool:
        raise DAGCreationError(f"PlanSpec node '{node.id}' must declare one concrete tool.")
    if node.tool == "dag_start":
        raise DAGCreationError("dag_start is reserved for the internal start node.")
    args = dict(node.args)
    registered = (tool_index or {}).get(node.tool)
    return DAGNode(
        id=node.id,
        invocation=CapabilityInvocation(
            capability_id=_tool_capability_id(node.tool),
            kind="tool",
            arguments=args,
            boundary=_infer_boundary(registered, args),
            risk=registered.policy.risk if registered is not None else "low",
        ),
    )


def _ensure_start_node(
    nodes: list[DAGNode],
    edges: list[DAGEdge],
) -> tuple[list[DAGNode], list[DAGEdge]]:
    node_ids = {node.id for node in nodes}
    start_id = "start"
    next_nodes = list(nodes)
    next_edges = list(edges)
    targets = {edge.target for edge in next_edges}
    existing_start_targets = {edge.target for edge in next_edges if edge.source == start_id}
    root_ids = [
        node.id
        for node in next_nodes
        if node.id != start_id and node.id not in targets and node.id not in existing_start_targets
    ]

    if start_id not in node_ids:
        next_nodes = [
            _start_node(),
            *next_nodes,
        ]
    elif not root_ids:
        return next_nodes, next_edges

    next_edges.extend(
        DAGEdge(
            source=start_id,
            target=node_id,
            reason=f"{node_id} starts after start.",
        )
        for node_id in root_ids
    )
    return next_nodes, next_edges


def _start_node() -> DAGNode:
    return DAGNode(
        id="start",
        node_type="start",
        invocation=CapabilityInvocation(
            capability_id="",
            kind="tool",
            arguments={},
            boundary=Boundary(mode="read_only"),
            risk="low",
        ),
    )


def _ensure_acyclic(node_ids: set[str], edges: list[tuple[str, str]]) -> None:
    outgoing: dict[str, list[str]] = defaultdict(list)
    indegree = {node_id: 0 for node_id in node_ids}

    for source, target in edges:
        outgoing[source].append(target)
        indegree[target] += 1

    queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
    visited_count = 0

    while queue:
        current = queue.popleft()
        visited_count += 1
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)

    if visited_count != len(node_ids):
        raise DAGValidationError("DAG must be acyclic.")


def _infer_boundary(tool_obj: CapabilityDefinition | None, args: dict[str, Any]) -> Boundary:
    if tool_obj is None:
        return Boundary(mode="read_only")
    path_args = tuple(tool_obj.config.get("path_args") or ())
    action = str(tool_obj.config.get("action") or "read")
    paths = [str(args.get(path_arg) or ".") for path_arg in path_args] or ["."]
    if action == "write":
        return Boundary(mode="write_limited", allowed_paths=paths)
    return Boundary(mode="read_only", allowed_paths=paths)


def _tool_capability_id(tool_name: str) -> str:
    return tool_name if tool_name.startswith("tool.") else f"tool.{tool_name}"
