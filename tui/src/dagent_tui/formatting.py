"""Terminal-oriented projections of DAG and trace payloads."""

from __future__ import annotations

from typing import Any

from rich.text import Text


_STATUS_GLYPHS = {
    "planned": "○",
    "ready": "◌",
    "running": "◉",
    "completed": "✓",
    "failed": "✗",
    "skipped": "–",
    "awaiting_review": "?",
}


def dag_text(dag: dict[str, Any] | None) -> Text:
    if not dag:
        return Text("No DAG yet.", style="dim")
    name = str(dag.get("name") or dag.get("dag_id") or dag.get("id") or "DAG")
    status = str(dag.get("status") or "")
    result = Text()
    result.append(name, style="bold cyan")
    if status:
        result.append(f"  [{status}]", style=_status_style(status))
    result.append("\n")
    nodes = dag.get("nodes")
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        node_status = str(node.get("status") or "planned")
        node_id = str(node.get("id") or "node")
        title = str(node.get("title") or "")
        result.append(f"{_STATUS_GLYPHS.get(node_status, '·')} ", style=_status_style(node_status))
        result.append(node_id, style="bold")
        if title and title != node_id:
            result.append(f" — {title}")
        target = _node_target(node)
        if target:
            result.append(f"\n    {target}", style="dim")
        result.append("\n")
    edges = dag.get("edges")
    if isinstance(edges, list) and edges:
        result.append("Edges\n", style="bold")
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            result.append(f"  {edge.get('source', '?')} → {edge.get('target', '?')}\n", style="dim")
    return result


def trace_text(trace: dict[str, Any] | None) -> Text:
    if not trace:
        return Text("No trace yet.", style="dim")
    result = Text("Trace\n", style="bold magenta")
    root = trace.get("root")
    if isinstance(root, dict):
        _append_trace_node(result, root, depth=0)
    return result


def activity_text(event_type: str, data: dict[str, Any]) -> Text:
    if event_type == "capability.call.started":
        capability = str(data.get("capability_id") or "capability")
        return Text.assemble(("◉ ", "yellow"), (capability, "bold"), " started")
    if event_type == "capability.call.completed":
        capability = str(data.get("capability_id") or "capability")
        return Text.assemble(("✓ ", "green"), (capability, "bold"), " completed")
    if event_type == "capability.call.failed":
        capability = str(data.get("capability_id") or "capability")
        content = str(data.get("content") or "")
        return Text.assemble(("✗ ", "red"), (capability, "bold"), f" {content}")
    if event_type == "validation.started":
        return Text("◌ validating response", style="yellow")
    if event_type == "validation.passed":
        return Text("✓ validation passed", style="green")
    if event_type == "validation.retry":
        reason = str(data.get("reason") or data.get("summary") or "retry requested")
        return Text(f"↻ validation retry: {reason}", style="yellow")
    if event_type == "review.required":
        return Text(f"? review required: {data.get('message', '')}", style="bold yellow")
    if event_type == "run.failed":
        return Text(f"✗ {data.get('message', 'Run failed.')}", style="bold red")
    return Text(event_type, style="dim")


def _append_trace_node(result: Text, node: dict[str, Any], *, depth: int) -> None:
    kind = str(node.get("kind") or "step")
    status = str(node.get("status") or "")
    label = _trace_label(node)
    result.append("  " * depth)
    result.append(f"{_STATUS_GLYPHS.get(status, '·')} ", style=_status_style(status))
    result.append(kind, style="bold")
    if label:
        result.append(f" {label}")
    if status:
        result.append(f" [{status}]", style=_status_style(status))
    result.append("\n")
    children = node.get("children")
    for child in children if isinstance(children, list) else []:
        if isinstance(child, dict):
            _append_trace_node(result, child, depth=depth + 1)


def _trace_label(node: dict[str, Any]) -> str:
    ref = node.get("ref")
    if not isinstance(ref, dict):
        return ""
    for key in ("node_id", "capability_id", "response_id", "dag_id"):
        if ref.get(key):
            return str(ref[key])
    return ""


def _node_target(node: dict[str, Any]) -> str:
    payload = node.get("payload")
    if not isinstance(payload, dict):
        return ""
    invocation = payload.get("invocation")
    if isinstance(invocation, dict) and invocation.get("capability_id"):
        return str(invocation["capability_id"])
    payload_type = payload.get("type")
    return str(payload_type) if payload_type else ""


def _status_style(status: str) -> str:
    if status == "completed":
        return "green"
    if status == "failed":
        return "red"
    if status in {"running", "ready"}:
        return "yellow"
    if status == "awaiting_review":
        return "bold yellow"
    return "dim"
