"""Fixtures for the internal typed dynamic-planner protocol."""

from __future__ import annotations

import json
from typing import Any, Iterable

from dagent.schemas import DAG, DAGSpec
from dagent.schemas import CapabilityInvocation, DAGNode
from dagent.schemas.node import (
    CapabilityNodePayload,
    ConditionNodePayload,
    StartNodePayload,
)
from dagent.schemas.value import parse_value_binding


def planner_value(value: Any) -> dict[str, Any]:
    expr = parse_value_binding(value)
    if expr is not None:
        payload = expr.model_dump(mode="json")
        expr_type = payload.pop("type")
        if expr_type == "format":
            payload["values"] = [
                {"name": name, "value": planner_value(item)}
                for name, item in payload["values"].items()
            ]
        elif expr_type == "compare":
            payload["left"] = planner_value(payload["left"])
            payload["right"] = planner_value(payload["right"])
        elif expr_type in {"all", "any"}:
            payload["values"] = [planner_value(item) for item in payload["values"]]
        elif expr_type == "not":
            payload["value"] = planner_value(payload["value"])
        return {"type": expr_type, **payload}
    if isinstance(value, list):
        return {"type": "list", "items": [planner_value(item) for item in value]}
    if isinstance(value, dict):
        return {
            "type": "object",
            "entries": [
                {"name": name, "value": planner_value(item)}
                for name, item in value.items()
            ],
        }
    return {"type": "literal", "value": value}


def planner_response_from_dag(
    dag: DAG,
    *,
    rerun_nodes: Iterable[str] = (),
) -> str:
    return _response(
        _graph(
            nodes=dag.nodes,
            edges=dag.edges,
            artifacts=[],
            output=None,
        ),
        rerun_nodes=rerun_nodes,
    )


def capability_plan_response(
    capability_id: str,
    arguments: dict[str, Any],
    *,
    node_id: str = "node_1",
    rerun_nodes: Iterable[str] = (),
) -> str:
    dag = DAG(
        dag_id="fixture",
        task_id="fixture",
        nodes=[DAGNode(
            id=node_id,
            payload={
                "type": "capability",
                "invocation": CapabilityInvocation(
                    capability_id=capability_id,
                    kind=capability_id.split(".", 1)[0],
                    arguments=arguments,
                ),
            },
        )],
    )
    return planner_response_from_dag(dag, rerun_nodes=rerun_nodes)


def planner_response_from_spec(
    spec: DAGSpec,
    *,
    rerun_nodes: Iterable[str] = (),
) -> str:
    return _response(_graph_from_spec(spec), rerun_nodes=rerun_nodes)


def no_change_response() -> str:
    return json.dumps({
        "action": "no_change",
        "plan": None,
        "answer": None,
        "rerun_nodes": [],
    })


def final_answer_response(answer: str) -> str:
    return json.dumps({
        "action": "final_answer",
        "plan": None,
        "answer": answer,
        "rerun_nodes": [],
    })


def _response(graph: dict[str, Any], *, rerun_nodes: Iterable[str]) -> str:
    return json.dumps({
        "action": "propose_plan",
        "plan": graph,
        "answer": None,
        "rerun_nodes": list(rerun_nodes),
    })


def _graph_from_spec(spec: DAGSpec) -> dict[str, Any]:
    return _graph(
        nodes=spec.nodes,
        edges=spec.edges,
        artifacts=[
            {
                "id": artifact.id,
                "paths": artifact.paths,
                "description": artifact.description,
                "required": artifact.required,
            }
            for artifact in spec.artifacts.values()
        ],
        output=None if spec.output is None else planner_value(spec.output),
    )


def _graph(
    *,
    nodes,
    edges,
    artifacts: list[dict[str, Any]],
    output: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "artifacts": artifacts,
        "nodes": [
            _node(node)
            for node in nodes
            if not isinstance(node.payload, StartNodePayload)
        ],
        "edges": [
            {
                "source": edge.source,
                "target": edge.target,
                "when": None if edge.when is None else planner_value(edge.when),
                "branch": edge.branch,
            }
            for edge in edges
            if edge.source != "start" and edge.target != "start"
        ],
        "output": output,
    }


def _node(node) -> dict[str, Any]:
    base = {
        "id": node.id,
        "inputs": node.inputs,
        "outputs": node.outputs,
    }
    payload = node.payload
    if isinstance(payload, CapabilityNodePayload):
        return {
            "type": "capability",
            **base,
            "capability_id": payload.invocation.capability_id,
            "arguments": _arguments(payload.invocation.arguments),
        }
    if isinstance(payload, ConditionNodePayload):
        return {
            "type": "condition",
            "id": node.id,
            "cases": [
                {"branch": case.branch, "when": planner_value(case.when)}
                for case in payload.cases
            ],
            "default_branch": payload.default_branch,
        }
    raise TypeError(f"Unsupported planner fixture node: {type(payload).__name__}")


def _arguments(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": name, "value": planner_value(value)}
        for name, value in arguments.items()
    ]
