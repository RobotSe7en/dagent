import asyncio
import json

import pytest
from jsonschema import Draft202012Validator

import dagent
from dagent.harness_runtime.builder_translator import (
    BuilderTranslationError,
    translate_builder_source,
)
from dagent.harness_runtime.dag_agent import (
    _budget_dag_observation,
    _edge_semantic_dump,
    _node_semantic_dump,
    _replan_spec_context,
)
from dagent.harness_runtime.dynamic_planner import normalize_builder_dag
from dagent.harness_runtime.planner_schema import (
    builder_planner_response_format,
    parse_builder_planner_response,
)
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import (
    CapabilityDefinition,
    CapabilityInvocation,
    CapabilityNodePayload,
    DAGEdge,
    DAGNode,
    DAGSpec,
)
from dagent.schemas.node import (
    ConditionNodePayload,
    LoopNodePayload,
    MapNodePayload,
    SubgraphNodePayload,
)
from dagent.schemas.value import bind_value_expr


def run(coro):
    return asyncio.run(coro)


def _builder_response(code: str, *, rerun_nodes=()) -> str:
    return json.dumps({
        "action": "propose_plan",
        "builder_code": code,
        "answer": None,
        "rerun_nodes": list(rerun_nodes),
    })


def _final_answer(answer: str) -> str:
    return json.dumps({
        "action": "final_answer",
        "builder_code": None,
        "answer": answer,
        "rerun_nodes": [],
    })


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
        ),
    ]


def test_builder_response_format_is_strict() -> None:
    response_format = builder_planner_response_format()

    Draft202012Validator.check_schema(response_format.schema)
    Draft202012Validator(response_format.schema).validate(json.loads(_builder_response(
        'dag = dagent.Dag("plan")\n'
        'work = dagent.Node("work", target="tool.echo", inputs={"text": "x"})\n'
        "dag.add_node(work)"
    )))
    parsed = parse_builder_planner_response(_final_answer("done"))
    assert parsed.answer == "done"


def test_builder_translation_normalizes_control_flow_and_host_owned_fields() -> None:
    source = '''
dag = dagent.Dag("model_root", name="Generated")
report = dag.artifact("report", ["report.md"], required=False)
seed = dagent.Node(
    "seed",
    target="tool.seed",
    inputs={},
    artifact_outputs=[report],
)
fanout = dagent.MapNode(
    "fanout",
    target="tool.echo",
    over=seed.output.items,
    inputs={"text": dag.format("item {value}", value=dagent.item)},
    max_items=8,
    max_concurrency=2,
)
child = dagent.Dag("model_child")
write = dagent.Node(
    "write",
    target="tool.echo",
    inputs={"text": child.input.text},
)
child.add_node(write)
child.output = write.output
subgraph = dagent.Node(
    "subgraph",
    target=child,
    inputs={"text": fanout.output[0]},
)
body = dagent.Dag("model_body")
refine = dagent.Node("refine", target="tool.echo", inputs={"text": body.input})
body.add_node(refine)
body.output = refine.output
repeat = dagent.LoopNode(
    "repeat",
    body=body,
    input=subgraph.output,
    until=dagent.item == "done",
    max_iterations=3,
)
dag.add_node(seed)
dag.add_node(fanout)
dag.add_node(subgraph)
dag.add_node(repeat)
dag.add_edge(seed, fanout, when=seed.output.ready == True)
dag.add_edge(fanout, subgraph)
dag.add_edge(subgraph, repeat)
dag.output = repeat.output
'''
    translated = translate_builder_source(
        source,
        capability_ids=[definition.id for definition in _capabilities()],
    )
    spec = normalize_builder_dag(
        translated,
        spec_id="host_task",
        version=7,
        capabilities=_capabilities(),
    )

    assert spec.id == "host_task"
    assert spec.version == 7
    assert spec.metadata == {}
    assert [node.id for node in spec.nodes] == [
        "start",
        "seed",
        "fanout",
        "subgraph",
        "repeat",
    ]
    fanout_payload = spec.nodes[2].payload
    subgraph_payload = spec.nodes[3].payload
    loop_payload = spec.nodes[4].payload
    assert isinstance(fanout_payload, MapNodePayload)
    assert isinstance(subgraph_payload, SubgraphNodePayload)
    assert isinstance(loop_payload, LoopNodePayload)
    assert subgraph_payload.spec.id == "host_task.subgraph.subgraph"
    assert subgraph_payload.spec.input_schema["properties"]["text"] == {"type": "string"}
    assert loop_payload.body.id == "host_task.repeat.loop"
    assert loop_payload.body.input_schema == {"type": "string"}
    assert spec.artifacts["report"].metadata == {}
    assert fanout_payload.invocation.kind == "tool"
    assert {(edge.source, edge.target) for edge in spec.edges} == {
        ("start", "seed"),
        ("seed", "fanout"),
        ("fanout", "subgraph"),
        ("subgraph", "repeat"),
    }


def test_builder_translation_supports_condition_nodes_and_logical_helpers() -> None:
    source = '''
dag = dagent.Dag("condition")
seed = dagent.Node("seed", target="tool.seed", inputs={})
route = dagent.ConditionNode(
    "route",
    cases=[
        dagent.Case(
            "ready",
            dagent.all_of(
                seed.output.ready == True,
                dagent.not_(seed.output.ready == False),
            ),
        ),
    ],
    default_branch="not_ready",
)
echo = dagent.Node("echo", target="tool.echo", inputs={"text": "ready"})
dag.add_node(seed)
dag.add_node(route)
dag.add_node(echo)
dag.add_edge(seed, route)
dag.add_edge(route, echo, branch="ready")
dag.output = route.output.branch
'''

    translated = translate_builder_source(
        source,
        capability_ids=[definition.id for definition in _capabilities()],
    )
    spec = normalize_builder_dag(
        translated,
        spec_id="condition",
        version=1,
        capabilities=_capabilities(),
    )

    route = next(node for node in spec.nodes if node.id == "route")
    assert isinstance(route.payload, ConditionNodePayload)
    assert route.payload.default_branch == "not_ready"
    assert next(edge for edge in spec.edges if edge.source == "route").branch == "ready"


def test_builder_full_spec_replan_preserves_invocation_identity() -> None:
    source = '''
dag = dagent.Dag("generated")
work = dagent.Node("work", target="tool.echo", inputs={"text": dag.input.request})
dag.add_node(work)
dag.output = work.output
'''
    first = normalize_builder_dag(
        translate_builder_source(source, capability_ids=["tool.echo"]),
        spec_id="task",
        version=1,
        capabilities=_capabilities(),
    )
    second = normalize_builder_dag(
        translate_builder_source(source, capability_ids=["tool.echo"]),
        spec_id="task",
        version=2,
        capabilities=_capabilities(),
        current=first,
    )

    assert first.nodes[-1].payload.invocation.invocation_id == (
        second.nodes[-1].payload.invocation.invocation_id
    )
    assert second.version == 2


def test_builder_validates_paths_through_composite_subgraph_output() -> None:
    capabilities = [
        CapabilityDefinition(
            id="tool.make",
            kind="tool",
            parameters={"type": "object", "additionalProperties": False},
            output_schema={
                "type": "object",
                "properties": {"inner": {"type": "string"}},
                "required": ["inner"],
                "additionalProperties": False,
            },
        ),
        *_capabilities(),
    ]
    source = '''
dag = dagent.Dag("generated")
child = dagent.Dag("child")
make = dagent.Node("make", target="tool.make", inputs={})
child.add_node(make)
child.output = {"data": make.output}
subgraph = dagent.Node("subgraph", target=child, inputs={})
consume = dagent.Node(
    "consume",
    target="tool.echo",
    inputs={"text": subgraph.output.data.inner},
)
dag.add_node(subgraph)
dag.add_node(consume)
dag.add_edge(subgraph, consume)
dag.output = consume.output
'''

    spec = normalize_builder_dag(
        translate_builder_source(
            source,
            capability_ids=[definition.id for definition in capabilities],
        ),
        spec_id="task",
        version=1,
        capabilities=capabilities,
    )

    assert isinstance(spec.nodes[1].payload, SubgraphNodePayload)
    assert spec.nodes[2].payload.invocation.arguments["text"]["$expr"]["path"] == [
        "data",
        "inner",
    ]


def test_replan_context_only_removes_host_fields_at_structural_paths() -> None:
    arguments = {
        "status": "user-status",
        "nested": {
            "metadata": {"source": "user"},
            "risk": "user-risk",
            "boundary": "user-boundary",
            "invocation_id": "user-invocation",
        },
    }
    spec = DAGSpec(
        id="task",
        name="task",
        metadata={"host": True},
        nodes=[
            DAGNode(
                id="work",
                status="completed",
                payload=CapabilityNodePayload(
                    type="capability",
                    invocation=CapabilityInvocation(
                        invocation_id="host-invocation",
                        capability_id="tool.echo",
                        kind="tool",
                        arguments=arguments,
                        risk="high",
                    ),
                ),
            )
        ],
        output={"metadata": "user-output", "status": "complete"},
    )

    dumped = json.loads(_replan_spec_context(spec))

    assert "metadata" not in dumped
    assert "status" not in dumped["nodes"][0]
    invocation = dumped["nodes"][0]["payload"]["invocation"]
    assert "invocation_id" not in invocation
    assert "boundary" not in invocation
    assert "risk" not in invocation
    assert invocation["arguments"] == arguments
    assert dumped["output"] == {
        "metadata": "user-output",
        "status": "complete",
    }


def test_node_semantic_dump_ignores_host_owned_identity_and_display_fields() -> None:
    first = DAGNode(
        id="work",
        payload=CapabilityNodePayload(
            type="capability",
            invocation=CapabilityInvocation(
                invocation_id="host-one",
                capability_id="tool.echo",
                kind="tool",
                arguments={"invocation_id": "user-one"},
            ),
        ),
    )
    second = first.model_copy(deep=True)
    second.payload.invocation.invocation_id = "host-two"
    second.title = "Updated display title"

    assert _node_semantic_dump(first) == _node_semantic_dump(second)

    second.payload.invocation.arguments["invocation_id"] = "user-two"
    assert _node_semantic_dump(first) != _node_semantic_dump(second)


def test_edge_semantic_dump_ignores_display_reason() -> None:
    first = DAGEdge(source="prepare", target="publish", reason="Initial reason")
    second = first.model_copy(update={"reason": "Updated reason"})

    assert _edge_semantic_dump(first) == _edge_semantic_dump(second)

    conditional = DAGEdge(
        source=second.source,
        target=second.target,
        reason=second.reason,
        when=bind_value_expr({"type": "graph_input", "path": ["enabled"]}),
    )
    assert _edge_semantic_dump(first) != _edge_semantic_dump(conditional)


def test_replan_observation_keeps_large_authoritative_spec_complete() -> None:
    large_argument = "x" * 20_000
    spec = DAGSpec(
        id="task",
        name="task",
        nodes=[
            DAGNode(
                id="work",
                payload=CapabilityNodePayload(
                    type="capability",
                    invocation=CapabilityInvocation(
                        capability_id="tool.echo",
                        kind="tool",
                        arguments={"text": large_argument},
                    ),
                ),
            )
        ],
    )
    spec_context = _replan_spec_context(spec)
    marker = "Current canonical DAGSpec (authoritative for replanning):\n"

    observation = _budget_dag_observation(
        [
            "DAG observation: layer_completed",
            "Task id: task",
            "Completed node outputs:\n" + ("y" * 20_000),
        ],
        marker + spec_context,
    )

    assert "[TRUNCATED" not in spec_context
    assert observation.startswith("DAG observation: layer_completed\n\nTask id: task")
    parsed = json.loads(observation.split(marker, 1)[1])
    assert parsed["nodes"][0]["payload"]["invocation"]["arguments"]["text"] == (
        large_argument
    )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("import os\ndag = dagent.Dag('x')", "Import"),
        ("dag = __import__('os')", "Only approved"),
        ("dag = dagent.Dag('x', version=99)", "host-owned"),
        (
            "dag = dagent.Dag('x')\n"
            "work = dagent.Node('work', target='tool.missing', inputs={})",
            "Unknown capability",
        ),
        (
            "dag = dagent.Dag('x')\n"
            "work = dagent.Node('work', target='skill.writer', inputs={})",
            "Skills cannot",
        ),
    ],
)
def test_builder_translation_rejects_noncanonical_or_host_owned_source(
    source: str,
    message: str,
) -> None:
    with pytest.raises(BuilderTranslationError, match=message):
        translate_builder_source(source, capability_ids=["tool.echo"])


def test_runner_sdk_builder_uses_frozen_skill_and_v4_checkpoint(tmp_path) -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return text

    code = '''
dag = dagent.Dag("generated")
work = dagent.Node(
    "work",
    target="tool.echo",
    inputs={"text": dag.input.request},
)
dag.add_node(work)
dag.output = work.output
'''
    provider = MockProvider([
        ChatResponse(content=_builder_response(code)),
        ChatResponse(content=_final_answer("done")),
    ])
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[echo],
        planner_frontend="sdk_builder",
    )

    result = run(runner.run(
        dagent.DagAgent(capabilities=[echo]),
        input="echo it",
    ))

    assert result.output_text == "done"
    assert result.state.schema_version == 4
    assert result.state.planner_frontend == "sdk_builder"
    assert result.plan is not None
    assert result.plan.schema_version == 7
    assert result.plan.max_steps == 888
    assert result.plan.runtime_directory == ".runtime"
    assert result.plan.planner_frontend == "sdk_builder"
    assert result.plan.planner_skill is not None
    assert result.plan.planner_skill.name == "generate-dag"
    assert result.checkpoint is not None
    assert result.checkpoint.schema_version == 7
    request = provider.requests[0]
    assert request["response_format"].name == "dagent_dynamic_dag_builder_response"
    assert request["response_format"].schema == builder_planner_response_format().schema
    system_prompt = request["messages"][0]["content"]
    assert "Mandatory Planner Skill" in system_prompt
    assert result.plan.planner_skill.content in system_prompt
    compact_schema = json.dumps(
        builder_planner_response_format().schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert "## Required Planner Response JSON Schema" in system_prompt
    assert compact_schema in system_prompt


def test_builder_checkpoint_resume_uses_frozen_frontend_and_skill(tmp_path) -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return text

    code = '''
dag = dagent.Dag("generated")
work = dagent.Node("work", target="tool.echo", inputs={"text": dag.input.request})
dag.add_node(work)
dag.output = work.output
'''
    provider = MockProvider([
        ChatResponse(content=_builder_response(code)),
        ChatResponse(content=_final_answer("resumed")),
    ])
    first_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[echo],
        planner_frontend="sdk_builder",
    )
    first = run(first_runner.run(
        dagent.DagAgent(capabilities=[echo], review="careful"),
        input="echo it",
    ))
    assert first.requires_review
    assert first.checkpoint is not None
    checkpoint = dagent.RunCheckpoint.model_validate_json(
        first.checkpoint.model_dump_json()
    )
    first_runner.close()

    second_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[echo],
        planner_frontend="typed_spec",
    )
    resumed = run(second_runner.resume(
        dagent.ReviewDecision(
            review_id=checkpoint.state.pending_review.review_id,
            approved=True,
        ),
        checkpoint=checkpoint,
    ))

    assert resumed is not None
    assert resumed.output_text == "resumed"
    assert resumed.plan is not None
    assert resumed.plan.planner_frontend == "sdk_builder"
    assert provider.requests[-1]["response_format"].name == (
        "dagent_dynamic_dag_builder_response"
    )
    assert checkpoint.plan.planner_skill is not None
    assert checkpoint.plan.planner_skill.content in (
        provider.requests[-1]["messages"][0]["content"]
    )


def test_builder_validation_error_uses_existing_planner_cycle(tmp_path) -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return text

    valid_code = '''
dag = dagent.Dag("generated")
work = dagent.Node("work", target="tool.echo", inputs={"text": dag.input.request})
dag.add_node(work)
dag.output = work.output
'''
    provider = MockProvider([
        ChatResponse(content=_builder_response(
            "import os\ndag = dagent.Dag('invalid')"
        )),
        ChatResponse(content=_builder_response(valid_code)),
        ChatResponse(content=_final_answer("repaired")),
    ])
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[echo],
        planner_frontend="sdk_builder",
    )

    result = run(runner.run(
        dagent.DagAgent(capabilities=[echo], max_steps=3),
        input="echo it",
    ))

    assert result.output_text == "repaired"
    assert len(provider.requests) == 3
    repair_prompt = provider.requests[1]["messages"][-1]["content"]
    assert "validation_error" in repair_prompt
    assert "Import" in repair_prompt
