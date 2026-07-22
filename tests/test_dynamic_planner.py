import json

import pytest
from jsonschema import Draft202012Validator

from dagent.harness_runtime.dag_builder import DAGCreationError, compile_dag_spec
from dagent.harness_runtime.dynamic_planner import normalize_planner_graph
from dagent.harness_runtime.planner_schema import (
    PlannerGraph,
    parse_planner_response,
    planner_response_format,
)
from dagent.schemas import CapabilityDefinition
from dagent.schemas.node import (
    CapabilityNodePayload,
    StartNodePayload,
)
from dagent.schemas.value import CompareExpr, NodeOutputExpr, parse_value_binding


def test_planner_response_format_is_strict_recursively() -> None:
    response_format = planner_response_format()

    assert response_format.strict is True
    assert response_format.name == "dagent_dynamic_dag_response"
    _assert_strict_objects(response_format.schema)


def test_planner_response_format_preserves_fields_and_normalizes_unions() -> None:
    response_format = planner_response_format()
    schema = response_format.schema

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(_response(_capability_graph()))

    node_definitions = [
        definition
        for definition in schema["$defs"].values()
        if {"id", "inputs", "outputs"}.issubset(
            definition.get("properties", {})
        )
    ]
    assert node_definitions
    assert all("title" not in definition["properties"] for definition in node_definitions)
    graph_definition = schema["$defs"]["PlannerGraph"]
    assert "name" not in graph_definition["properties"]
    assert "description" not in graph_definition["properties"]
    edge_definition = schema["$defs"]["PlannerEdge"]
    assert "reason" not in edge_definition["properties"]
    assert "PlannerMapNode" not in schema["$defs"]
    assert "PlannerSubgraphNode" not in schema["$defs"]
    assert "PlannerLoopNode" not in schema["$defs"]
    assert "PlannerItemValue" not in schema["$defs"]
    assert not _schema_contains_key(schema, "oneOf")
    assert not _schema_contains_key(schema, "discriminator")
    assert _schema_contains_key(schema, "anyOf")


def test_planner_response_rejects_unknown_fields_and_inconsistent_actions() -> None:
    response = _response(_capability_graph())
    response["unexpected"] = True

    with pytest.raises(ValueError, match="unexpected"):
        parse_planner_response(json.dumps(response))

    with pytest.raises(ValueError, match="no_change forbids"):
        parse_planner_response(json.dumps({
            "action": "no_change",
            "plan": _capability_graph(),
            "answer": None,
            "rerun_nodes": [],
        }))


@pytest.mark.parametrize("node_type", ["map", "subgraph", "loop"])
def test_typed_planner_rejects_complex_node_types(node_type: str) -> None:
    graph = _single_node_graph()
    graph["nodes"][0]["type"] = node_type

    with pytest.raises(ValueError, match="capability"):
        parse_planner_response(json.dumps(_response(graph)))


def test_typed_planner_normalizes_capability_graph_to_canonical_dag_spec() -> None:
    parsed = parse_planner_response(json.dumps(_response(_capability_graph())))
    assert parsed.plan is not None

    spec = normalize_planner_graph(
        parsed.plan,
        spec_id="task_control",
        version=1,
        capabilities=_capabilities(),
    )
    dag = compile_dag_spec(spec, task_id="task_control", capabilities=_capabilities())

    assert spec.id == "task_control"
    assert spec.name == "task_control"
    assert spec.description == ""
    assert spec.input_schema["required"] == ["request"]
    assert list(spec.artifacts) == ["report"]
    assert [node.id for node in spec.nodes] == [
        "start",
        "seed",
        "echo",
    ]
    assert isinstance(spec.nodes[0].payload, StartNodePayload)
    assert isinstance(spec.nodes[1].payload, CapabilityNodePayload)
    assert isinstance(spec.nodes[2].payload, CapabilityNodePayload)
    assert all(node.title == "" for node in spec.nodes)
    assert all(edge.reason == "" for edge in spec.edges)
    assert {(edge.source, edge.target) for edge in spec.edges} == {
        ("start", "seed"),
        ("seed", "echo"),
    }
    condition = next(edge.when for edge in spec.edges if edge.target == "echo")
    assert isinstance(parse_value_binding(condition), CompareExpr)
    assert dag.nodes[1].payload.invocation.capability_id == "tool.seed"
    assert dag.nodes[2].payload.invocation.risk == "medium"
    text = dag.nodes[2].payload.invocation.arguments["text"]
    assert isinstance(parse_value_binding(text), NodeOutputExpr)


def test_planner_argument_schema_validation_fails_closed_but_allows_expressions() -> None:
    invalid = _single_node_graph(
        arguments=[{"name": "text", "value": _literal(7)}]
    )
    with pytest.raises(DAGCreationError, match="invalid arguments.*not of type 'string'"):
        normalize_planner_graph(
            PlannerGraph.model_validate(invalid),
            spec_id="invalid",
            version=1,
            capabilities=_capabilities(),
        )

    expression = _single_node_graph(arguments=[{
        "name": "text",
        "value": {"type": "graph_input", "path": ["request"]},
    }])
    spec = normalize_planner_graph(
        PlannerGraph.model_validate(expression),
        spec_id="expression",
        version=1,
        capabilities=_capabilities(),
    )

    assert parse_value_binding(spec.nodes[-1].payload.invocation.arguments["text"])


def test_planner_validates_output_paths_through_local_refs_and_unions() -> None:
    graph = _single_node_graph(
        capability_id="tool.structured",
        arguments=[],
    )
    graph["output"] = {
        "type": "node_output",
        "node_id": "work",
        "field": "value",
        "path": ["result", "name"],
    }
    structured = CapabilityDefinition(
        id="tool.structured",
        kind="tool",
        parameters={"type": "object", "additionalProperties": False},
        output_schema={
            "$defs": {
                "Payload": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }
            },
            "type": "object",
            "properties": {
                "result": {
                    "anyOf": [
                        {"$ref": "#/$defs/Payload"},
                        {"type": "null"},
                    ]
                }
            },
            "required": ["result"],
        },
    )

    spec = normalize_planner_graph(
        PlannerGraph.model_validate(graph),
        spec_id="structured",
        version=1,
        capabilities=[structured],
    )
    assert spec.output is not None

    graph["output"]["path"] = ["result", "missing"]
    with pytest.raises(DAGCreationError, match="unknown output path 'result.missing'"):
        normalize_planner_graph(
            PlannerGraph.model_validate(graph),
            spec_id="structured",
            version=1,
            capabilities=[structured],
        )


def test_full_spec_replan_preserves_unchanged_invocation_identity() -> None:
    graph = PlannerGraph.model_validate(_capability_graph())
    first = normalize_planner_graph(
        graph,
        spec_id="stable",
        version=1,
        capabilities=_capabilities(),
    )
    second = normalize_planner_graph(
        graph,
        spec_id="stable",
        version=2,
        capabilities=_capabilities(),
        current=first,
    )

    first_ids = _invocation_ids(first)
    second_ids = _invocation_ids(second)
    assert second_ids == first_ids
    assert second.version == 2


def _capability_graph() -> dict:
    return {
        "artifacts": [{
            "id": "report",
            "paths": ["out/report.json"],
            "description": "Optional report.",
            "required": False,
        }],
        "nodes": [
            {
                **_node_base("seed"),
                "type": "capability",
                "capability_id": "tool.seed",
                "arguments": [],
                "outputs": ["report"],
            },
            {
                **_node_base("echo"),
                "type": "capability",
                "capability_id": "tool.echo",
                "arguments": [{"name": "text", "value": _node_output("seed", path=["items", 0])}],
                "inputs": ["report"],
            },
        ],
        "edges": [
            {
                "source": "seed",
                "target": "echo",
                "when": {
                    "type": "compare",
                    "op": "eq",
                    "left": _node_output("seed", path=["ready"]),
                    "right": _literal(True),
                },
            },
        ],
        "output": _node_output("echo"),
    }


def _single_node_graph(
    *,
    node_id: str = "work",
    capability_id: str = "tool.echo",
    arguments: list[dict] | None = None,
) -> dict:
    return {
        "artifacts": [],
        "nodes": [{
            **_node_base(node_id),
            "type": "capability",
            "capability_id": capability_id,
            "arguments": arguments or [],
        }],
        "edges": [],
        "output": None,
    }


def _node_base(node_id: str) -> dict:
    return {"id": node_id, "inputs": [], "outputs": []}


def _node_output(node_id: str, *, path: list | None = None) -> dict:
    return {
        "type": "node_output",
        "node_id": node_id,
        "field": "value",
        "path": path or [],
    }


def _literal(value) -> dict:
    return {"type": "literal", "value": value}


def _response(plan: dict) -> dict:
    return {
        "action": "propose_plan",
        "plan": plan,
        "answer": None,
        "rerun_nodes": [],
    }


def _capabilities() -> list[CapabilityDefinition]:
    return [
        CapabilityDefinition(
            id="tool.seed",
            kind="tool",
            parameters={"type": "object", "additionalProperties": False},
            output_schema={
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"type": "string"}},
                    "ready": {"type": "boolean"},
                },
                "required": ["items", "ready"],
            },
        ),
        CapabilityDefinition(
            id="tool.echo",
            kind="tool",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            output_schema={"type": "string"},
            policy={"risk": "medium"},
        ),
    ]


def _invocation_ids(spec) -> dict[str, str]:
    return {
        f"{spec.id}:{node.id}": node.payload.invocation.invocation_id
        for node in spec.nodes
        if isinstance(node.payload, CapabilityNodePayload)
    }


def _assert_strict_objects(value) -> None:
    if isinstance(value, list):
        for item in value:
            _assert_strict_objects(item)
        return
    if not isinstance(value, dict):
        return
    if value.get("type") == "object" or "properties" in value:
        assert value.get("additionalProperties") is False
        assert set(value.get("required", [])) == set(value.get("properties", {}))
    for item in value.values():
        _assert_strict_objects(item)


def _schema_contains_key(value, key: str) -> bool:
    if isinstance(value, list):
        return any(_schema_contains_key(item, key) for item in value)
    if not isinstance(value, dict):
        return False
    return key in value or any(
        _schema_contains_key(item, key)
        for item in value.values()
    )
