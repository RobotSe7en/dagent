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
    LoopNodePayload,
    MapNodePayload,
    StartNodePayload,
    SubgraphNodePayload,
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
    Draft202012Validator(schema).validate(_response(_control_flow_graph()))

    node_definitions = [
        definition
        for definition in schema["$defs"].values()
        if {"id", "title", "inputs", "outputs"}.issubset(
            definition.get("properties", {})
        )
    ]
    assert node_definitions
    assert all("title" in definition["required"] for definition in node_definitions)
    assert not _schema_contains_key(schema, "oneOf")
    assert not _schema_contains_key(schema, "discriminator")
    assert _schema_contains_key(schema, "anyOf")


def test_planner_response_rejects_unknown_fields_and_inconsistent_actions() -> None:
    response = _response(_control_flow_graph())
    response["unexpected"] = True

    with pytest.raises(ValueError, match="unexpected"):
        parse_planner_response(json.dumps(response))

    with pytest.raises(ValueError, match="no_change forbids"):
        parse_planner_response(json.dumps({
            "action": "no_change",
            "plan": _control_flow_graph(),
            "answer": None,
            "rerun_nodes": [],
        }))


def test_typed_planner_normalizes_all_control_flow_to_canonical_dag_spec() -> None:
    parsed = parse_planner_response(json.dumps(_response(_control_flow_graph())))
    assert parsed.plan is not None

    spec = normalize_planner_graph(
        parsed.plan,
        spec_id="task_control",
        version=1,
        capabilities=_capabilities(),
    )
    dag = compile_dag_spec(spec, task_id="task_control", capabilities=_capabilities())

    assert spec.id == "task_control"
    assert spec.input_schema["required"] == ["request"]
    assert list(spec.artifacts) == ["report"]
    assert [node.id for node in spec.nodes] == [
        "start",
        "seed",
        "fan_out",
        "summarize",
        "repeat",
    ]
    assert isinstance(spec.nodes[0].payload, StartNodePayload)
    assert isinstance(spec.nodes[1].payload, CapabilityNodePayload)
    assert isinstance(spec.nodes[2].payload, MapNodePayload)
    assert isinstance(spec.nodes[3].payload, SubgraphNodePayload)
    assert isinstance(spec.nodes[4].payload, LoopNodePayload)
    assert {(edge.source, edge.target) for edge in spec.edges} == {
        ("start", "seed"),
        ("seed", "fan_out"),
        ("fan_out", "summarize"),
        ("summarize", "repeat"),
    }
    condition = next(edge.when for edge in spec.edges if edge.target == "fan_out")
    assert isinstance(parse_value_binding(condition), CompareExpr)
    items = spec.nodes[2].payload.items
    assert isinstance(parse_value_binding(items), NodeOutputExpr)
    assert dag.nodes[1].payload.invocation.capability_id == "tool.seed"
    assert dag.nodes[2].payload.invocation.risk == "medium"


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
    graph = PlannerGraph.model_validate(_control_flow_graph())
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


def test_nested_graph_input_paths_are_validated_from_subgraph_input() -> None:
    nested = _single_node_graph(arguments=[{
        "name": "text",
        "value": {"type": "graph_input", "path": ["missing"]},
    }])
    graph = {
        "name": "nested input",
        "description": "",
        "artifacts": [],
        "nodes": [{
            **_node_base("nested"),
            "type": "subgraph",
            "graph": nested,
            "input": {
                "type": "object",
                "entries": [{"name": "present", "value": _literal("value")}],
            },
        }],
        "edges": [],
        "output": None,
    }

    with pytest.raises(DAGCreationError, match="unknown output path 'missing'"):
        normalize_planner_graph(
            PlannerGraph.model_validate(graph),
            spec_id="nested",
            version=1,
            capabilities=_capabilities(),
        )


def test_nested_graph_input_path_requires_an_input_schema() -> None:
    nested = _single_node_graph(arguments=[{
        "name": "text",
        "value": {"type": "graph_input", "path": ["missing"]},
    }])
    graph = {
        "name": "missing nested input",
        "description": "",
        "artifacts": [],
        "nodes": [{
            **_node_base("nested"),
            "type": "subgraph",
            "graph": nested,
            "input": None,
        }],
        "edges": [],
        "output": None,
    }

    with pytest.raises(DAGCreationError, match="source has no output schema"):
        normalize_planner_graph(
            PlannerGraph.model_validate(graph),
            spec_id="nested",
            version=1,
            capabilities=_capabilities(),
        )


def _control_flow_graph() -> dict:
    nested = _single_node_graph(
        node_id="nested_echo",
        arguments=[{
            "name": "text",
            "value": {"type": "graph_input", "path": []},
        }],
        name="nested",
    )
    nested["output"] = _node_output("nested_echo")
    body = _single_node_graph(
        node_id="loop_echo",
        arguments=[{
            "name": "text",
            "value": {"type": "graph_input", "path": []},
        }],
        name="loop body",
    )
    body["output"] = _node_output("loop_echo")
    return {
        "name": "control flow",
        "description": "Exercise typed dynamic nodes.",
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
                **_node_base("fan_out"),
                "type": "map",
                "items": _node_output("seed", path=["items"]),
                "capability_id": "tool.echo",
                "arguments": [{"name": "text", "value": {"type": "item", "path": []}}],
                "max_items": 10,
                "max_concurrency": 3,
                "inputs": ["report"],
            },
            {
                **_node_base("summarize"),
                "type": "subgraph",
                "graph": nested,
                "input": _node_output("fan_out", path=[0]),
            },
            {
                **_node_base("repeat"),
                "type": "loop",
                "body": body,
                "until": {
                    "type": "compare",
                    "op": "eq",
                    "left": {"type": "item", "path": []},
                    "right": _literal("done"),
                },
                "max_iterations": 3,
                "input": _node_output("summarize"),
            },
        ],
        "edges": [
            {
                "source": "seed",
                "target": "fan_out",
                "reason": "Fan out only when ready.",
                "when": {
                    "type": "compare",
                    "op": "eq",
                    "left": _node_output("seed", path=["ready"]),
                    "right": _literal(True),
                },
            },
            {"source": "fan_out", "target": "summarize", "reason": "", "when": None},
            {"source": "summarize", "target": "repeat", "reason": "", "when": None},
        ],
        "output": _node_output("repeat"),
    }


def _single_node_graph(
    *,
    node_id: str = "work",
    capability_id: str = "tool.echo",
    arguments: list[dict] | None = None,
    name: str = "single",
) -> dict:
    return {
        "name": name,
        "description": "",
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
    return {"id": node_id, "title": "", "inputs": [], "outputs": []}


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
    output: dict[str, str] = {}
    stack = [spec]
    while stack:
        current = stack.pop()
        for node in current.nodes:
            payload = node.payload
            if isinstance(payload, (CapabilityNodePayload, MapNodePayload)):
                output[f"{current.id}:{node.id}"] = payload.invocation.invocation_id
            elif isinstance(payload, SubgraphNodePayload):
                stack.append(payload.spec)
            elif isinstance(payload, LoopNodePayload):
                stack.append(payload.body)
    return output


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
