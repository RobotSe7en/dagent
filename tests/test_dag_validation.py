import asyncio

import pytest

from dagent.harness_runtime import DAGAgent, DAGAgentLoop, DAGExecutor
from dagent.harness_runtime import CapabilityExecutor
from dagent.harness_runtime.dag_builder import (
    DAGCreationError,
    DAGValidationError,
    compile_dag_spec,
    validate_dag,
)
from dagent.harness_runtime.dynamic_planner import normalize_planner_graph
from dagent.harness_runtime.planner_schema import PlannerGraph, parse_planner_response
from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import DAG, DAGEdge, DAGNode, CapabilityDefinition, CapabilityInvocation, RunState
from dagent.schemas.value import ValueExpressionError, parse_value_binding
from dagent.capabilities.tools.registry import ToolRegistry
from tests.planner_helpers import planner_response_from_dag


def make_node(node_id: str) -> DAGNode:
    return DAGNode(
        id=node_id,
        payload=dict(
            type="capability",
            invocation=CapabilityInvocation(
                capability_id="tool.echo",
                kind="tool",
                arguments={"text": node_id},
            ),
        ),
    )


def dag_state(*, task_id: str, user_request: str, dag: DAG) -> RunState:
    return RunState(
        run_id=task_id,
        kind="dynamic_dag",
        status="completed",
        user_request=user_request,
        dag=dag,
        runtime_mode="dag",
    )


def test_valid_dag_passes_validation() -> None:
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[make_node("a"), make_node("b")],
        edges=[DAGEdge(source="a", target="b")],
    )

    validate_dag(dag)


def test_format_value_expression_rejects_unbound_template_fields() -> None:
    with pytest.raises(ValueExpressionError, match="missing values: query"):
        parse_value_binding({
            "$expr": {
                "type": "format",
                "template": "Question: {query}",
                "values": {},
            }
        })


def test_format_value_expression_allows_literal_braces() -> None:
    expression = parse_value_binding({
        "$expr": {
            "type": "format",
            "template": 'Question: {query}; JSON: {{"enabled": true}}',
            "values": {"query": "dagent"},
        }
    })

    assert expression is not None


def test_dag_must_have_at_least_one_node() -> None:
    dag = DAG(dag_id="dag_1", task_id="task_1")

    with pytest.raises(DAGValidationError, match="at least one node"):
        validate_dag(dag)


def test_node_ids_must_be_unique() -> None:
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[make_node("a"), make_node("a")],
    )

    with pytest.raises(DAGValidationError, match="Duplicate node IDs: a"):
        validate_dag(dag)


def test_agent_nodes_are_rejected_for_executable_dags() -> None:
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            DAGNode(
                id="reason",
                payload=dict(
                    type="capability",
                    invocation=CapabilityInvocation(capability_id="", kind="tool"),
                ),
            )
        ],
    )

    with pytest.raises(DAGValidationError, match="must declare a capability"):
        validate_dag(dag)


def test_edge_source_must_exist() -> None:
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[make_node("b")],
        edges=[DAGEdge(source="a", target="b")],
    )

    with pytest.raises(DAGValidationError, match="source 'a'"):
        validate_dag(dag)


def test_edge_target_must_exist() -> None:
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[make_node("a")],
        edges=[DAGEdge(source="a", target="b")],
    )

    with pytest.raises(DAGValidationError, match="target 'b'"):
        validate_dag(dag)


def test_multi_node_dag_rejects_isolated_nodes() -> None:
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[make_node("a"), make_node("b"), make_node("lonely")],
        edges=[DAGEdge(source="a", target="b")],
    )

    with pytest.raises(DAGValidationError, match="Isolated node IDs: lonely"):
        validate_dag(dag)


def test_multi_node_dag_without_edges_is_rejected() -> None:
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[make_node("a"), make_node("b")],
        edges=[],
    )

    with pytest.raises(DAGValidationError, match="Isolated node IDs: a, b"):
        validate_dag(dag)


def test_compile_inserts_internal_start_node_for_parallel_roots() -> None:
    spec = _normalize_graph({
        "name": "parallel",
        "nodes": [
            _planner_capability_node("a", "tool.echo", text="a"),
            _planner_capability_node("b", "tool.echo", text="b"),
        ],
    })

    dag = compile_dag_spec(spec, task_id="task_1", capabilities=[_capability("tool.echo")])

    start = dag.nodes[0]
    assert start.id == "start"
    assert start.payload.type == "start"
    dumped = start.model_dump(mode="json")
    assert dumped["payload"] == {"type": "start"}
    assert "node_type" not in dumped
    assert "invocation" not in dumped
    assert {edge.source for edge in dag.edges} == {"start"}
    assert {edge.target for edge in dag.edges} == {"a", "b"}
    validate_dag(dag)


def test_explicit_dag_start_is_rejected_as_reserved_internal_node() -> None:
    graph = _planner_graph({
        "name": "explicit start",
        "nodes": [_planner_capability_node("start", "tool.echo", text="a")],
    })

    with pytest.raises(ValueError, match="reserved"):
        PlannerGraph.model_validate(graph)


def test_normalizer_rejects_unknown_capability_id() -> None:
    with pytest.raises(
        DAGCreationError,
        match="Unknown capability 'tool.missing'. Available capabilities: tool.echo.",
    ):
        _normalize_graph({
            "name": "unknown",
            "nodes": [_planner_capability_node("missing", "tool.missing", text="a")],
        })


def test_normalizer_uses_registered_non_tool_capability() -> None:
    spec = _normalize_graph(
        {
            "name": "memory read",
            "nodes": [_planner_capability_node("read", "memory.read", key="notes")],
        },
        capabilities=[_capability("memory.read", kind="memory")],
    )

    node = spec.nodes[1]
    assert node.payload.invocation.capability_id == "memory.read"
    assert node.payload.invocation.kind == "memory"


def test_normalizer_uses_stable_capability_id_not_function_name() -> None:
    spec = _normalize_graph(
        {
            "name": "named tool",
            "nodes": [_planner_capability_node("lookup", "tool.lookup", query="dagent")],
        },
        capabilities=[CapabilityDefinition(id="tool.lookup", kind="tool", name="search")],
    )

    node = spec.nodes[1]
    assert node.payload.invocation.capability_id == "tool.lookup"


def test_normalizer_infers_boundary_for_command_capability() -> None:
    spec = _normalize_graph(
        {
            "name": "run command",
            "nodes": [_planner_capability_node(
                "run",
                "tool.shell",
                command='node -e "console.log(1);"',
                cwd=".",
            )],
        },
        capabilities=[
            CapabilityDefinition(
                id="tool.shell",
                kind="tool",
                config={
                    "action": "command",
                    "path_args": ["cwd"],
                    "command_args": ["command"],
                    "default_args": {"cwd": "."},
                },
            )
        ],
    )

    boundary = spec.nodes[1].payload.invocation.boundary
    assert boundary.allowed_paths == ["."]


def test_planner_schema_rejects_node_goal_and_instructions() -> None:
    graph = _planner_graph({
        "name": "requirements",
        "nodes": [{
            **_planner_capability_node(
                "write_requirements",
                "agent.helper",
                prompt="Write a requirement specification.",
            ),
            "goal": "Write a requirement specification.",
            "instructions": "Use acceptance criteria.",
        }],
    })
    with pytest.raises(ValueError):
        PlannerGraph.model_validate(graph)


def test_normalizer_preserves_agent_prompt_argument() -> None:
    spec = _normalize_graph(
        {
            "name": "requirements",
            "nodes": [_planner_capability_node(
                "write_requirements",
                "agent.helper",
                prompt="Write a requirement specification. Use acceptance criteria.",
            )],
        },
        capabilities=[_capability("agent.helper", kind="agent")],
    )

    assert spec.nodes[1].payload.invocation.arguments == {
        "prompt": "Write a requirement specification. Use acceptance criteria."
    }


def test_dag_must_be_acyclic() -> None:
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[make_node("a"), make_node("b"), make_node("c")],
        edges=[
            DAGEdge(source="a", target="b"),
            DAGEdge(source="b", target="c"),
            DAGEdge(source="c", target="a"),
        ],
    )

    with pytest.raises(DAGValidationError, match="acyclic"):
        validate_dag(dag)


def test_llm_dag_agent_with_mock_provider_returns_valid_dag() -> None:
    planned = DAG(
        dag_id="fixture",
        task_id="fixture",
        nodes=[DAGNode(
            id="inspect",
            payload=dict(
                type="capability",
                invocation=CapabilityInvocation(
                    capability_id="tool.shell",
                    kind="tool",
                    arguments={"command": "dir", "cwd": "."},
                ),
            ),
        )],
    )
    provider = MockProvider(
        [
            ChatResponse(content=planner_response_from_dag(planned))
        ]
    )
    registry = ToolRegistry()
    registry.register(
        name="shell",
        handler=lambda command, cwd=".": f"ran:{command}",
        action="read",
        parameters={"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}}},
    )
    capability_catalog = CapabilityCatalog()
    capability_executor = CapabilityExecutor(capability_catalog)
    ToolCapabilityProvider(registry).register_into(capability_catalog)
    tool_adapter = CapabilityToolAdapter(
        capability_catalog,
        toolsets=[CapabilityToolset("builtin", tuple(sorted(capability_catalog.ids())))],
    )
    loop = DAGAgentLoop(
        provider=provider,
        dag_executor=DAGExecutor(capability_executor=capability_executor),
        tool_adapter=tool_adapter,
    )
    agent = DAGAgent(
        loop=loop,
        profile=_dag_agent_profile(),
    )

    messages = [agent.system_message]
    requested = asyncio.run(loop._request_dag(
        task_id="task_1",
        messages=messages,
        user_message=agent.build_request_user_message(
            prompt="Summarize the repo",
            task_id="task_1",
        ),
        allow_no_change=False,
    ))
    dag = compile_dag_spec(
        requested.spec,
        task_id="task_1",
        capabilities=loop.available_capabilities(),
    )

    validate_dag(dag)
    assert dag.task_id == "task_1"
    invocations = [
        node.payload.invocation
        for node in dag.nodes
        if hasattr(node.payload, "invocation")
    ]
    assert [invocation.capability_id for invocation in invocations] == ["tool.shell"]
    assert [invocation.risk for invocation in invocations] == ["low"]


def test_dag_agent_rejects_capability_outside_enabled_toolset() -> None:
    provider = MockProvider([ChatResponse(content="unused")])
    registry = ToolRegistry()
    registry.register(name="echo", handler=lambda text: text, action="read")
    registry.register(name="write_file", handler=lambda path, content="": content, action="write")
    capability_catalog = CapabilityCatalog()
    capability_executor = CapabilityExecutor(capability_catalog)
    ToolCapabilityProvider(registry).register_into(capability_catalog)
    loop = DAGAgentLoop(
        provider=provider,
        dag_executor=DAGExecutor(capability_executor=capability_executor),
        tool_adapter=CapabilityToolAdapter(
            capability_catalog,
            toolsets=[CapabilityToolset("builtin", ("tool.echo",))],
        ),
    )
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            DAGNode(
                id="write",
                payload=dict(
                    type="capability",
                    invocation=CapabilityInvocation(
                        capability_id="tool.write_file",
                        kind="tool",
                        arguments={"path": "notes.txt", "content": "hi"},
                    ),
                ),
            )
        ],
    )

    with pytest.raises(DAGValidationError, match="Unknown capability\\(s\\): tool.write_file"):
        loop.prepare_for_review(dag)


def test_dag_agent_execute_rejects_capability_outside_enabled_toolset() -> None:
    provider = MockProvider([ChatResponse(content="unused")])
    registry = ToolRegistry()
    registry.register(name="echo", handler=lambda text: text, action="read")
    registry.register(name="write_file", handler=lambda path, content="": content, action="write")
    capability_catalog = CapabilityCatalog()
    capability_executor = CapabilityExecutor(capability_catalog)
    ToolCapabilityProvider(registry).register_into(capability_catalog)
    loop = DAGAgentLoop(
        provider=provider,
        dag_executor=DAGExecutor(capability_executor=capability_executor),
        tool_adapter=CapabilityToolAdapter(
            capability_catalog,
            toolsets=[CapabilityToolset("builtin", ("tool.echo",))],
        ),
    )
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="approved",
        nodes=[
            DAGNode(
                id="write",
                payload=dict(
                    type="capability",
                    invocation=CapabilityInvocation(
                        capability_id="tool.write_file",
                        kind="tool",
                        arguments={"path": "notes.txt", "content": "hi"},
                    ),
                ),
            )
        ],
    )
    record = dag_state(
        task_id="task_1",
        user_request="write",
        dag=dag,
    )

    with pytest.raises(DAGValidationError, match="Unknown capability\\(s\\): tool.write_file"):
        asyncio.run(
            loop.execute(
                record,
                messages=[],
                build_user_message=lambda **_: {"role": "user", "content": ""},
            )
        )


def test_dag_agent_execute_rejects_entry_observation_without_replanning() -> None:
    provider = MockProvider([])
    capability_executor = CapabilityExecutor(CapabilityCatalog())
    loop = DAGAgentLoop(
        provider=provider,
        dag_executor=DAGExecutor(capability_executor=capability_executor),
        tool_adapter=CapabilityToolAdapter(
            capability_executor.catalog,
            toolsets=[CapabilityToolset("builtin", ())],
        ),
    )
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="approved",
        nodes=[
            DAGNode(
                id="start",
                payload=dict(type="start"),
            )
        ],
    )
    record = dag_state(
        task_id="task_1",
        user_request="plan",
        dag=dag,
    )

    with pytest.raises(TypeError, match="entry_observation requires replan=True"):
        asyncio.run(
            loop.execute(
                record,
                replan=False,
                entry_observation="plan this",
            )
        )


def _dag_agent_profile() -> AgentProfile:
    return AgentProfile(
        name="dag_agent",
        content="You are a DAG creator.",
    )


def _capability(capability_id: str, *, kind: str = "tool") -> CapabilityDefinition:
    return CapabilityDefinition(id=capability_id, kind=kind)


def _planner_capability_node(node_id: str, capability_id: str, **arguments):
    return {
        "type": "capability",
        "id": node_id,
        "title": "",
        "inputs": [],
        "outputs": [],
        "capability_id": capability_id,
        "arguments": [
            {"name": name, "value": {"type": "literal", "value": value}}
            for name, value in arguments.items()
        ],
    }


def _planner_graph(overrides: dict) -> dict:
    return {
        "name": "test",
        "description": "",
        "artifacts": [],
        "nodes": [],
        "edges": [],
        "output": None,
        **overrides,
    }


def _normalize_graph(
    graph: dict,
    *,
    capabilities: list[CapabilityDefinition] | None = None,
):
    return normalize_planner_graph(
        PlannerGraph.model_validate(_planner_graph(graph)),
        spec_id="task_1",
        version=1,
        capabilities=capabilities or [_capability("tool.echo")],
    )
