import asyncio

import pytest

from dagent.harness_runtime import DAGAgent, DAGAgentLoop, DAGExecutor
from dagent.harness_runtime import CapabilityExecutor
from dagent.harness_runtime.dag_builder import (
    DAGCreationError,
    DAGValidationError,
    compile_plan_spec,
    parse_plan_spec_dsl,
    validate_dag,
)
from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import DAG, DAGEdge, DAGNode, CapabilityDefinition, CapabilityInvocation, RunState
from dagent.schemas.dag import PlanSpec
from dagent.capabilities.tools.registry import ToolRegistry


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
    plan = parse_plan_spec_dsl(
        'task: parallel\n'
        'a = echo(text="a")\n'
        'b = echo(text="b")\n'
    )

    dag = compile_plan_spec(plan, task_id="task_1", tools=[_capability("echo", "tool.echo")])

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
    plan = parse_plan_spec_dsl(
        'task: explicit start\n'
        'start = dag_start()\n'
        'a = echo(text="a") after start\n'
    )

    with pytest.raises(DAGCreationError, match="reserved"):
        compile_plan_spec(plan, task_id="task_1")


def test_compile_rejects_unknown_capability_function_name() -> None:
    plan = parse_plan_spec_dsl(
        'task: unknown\n'
        'missing = missing_tool(text="a")\n'
    )

    with pytest.raises(
        DAGCreationError,
        match="Unknown capability function 'missing_tool'. Available functions: echo.",
    ):
        compile_plan_spec(plan, task_id="task_1", tools=[_capability("echo", "tool.echo")])


def test_compile_uses_registered_non_tool_capability_mapping() -> None:
    plan = parse_plan_spec_dsl(
        'task: memory read\n'
        'read = memory_read(key="notes")\n'
    )

    dag = compile_plan_spec(
        plan,
        task_id="task_1",
        tools=[_capability("memory_read", "memory.read", kind="memory")],
    )

    node = dag.nodes[0]
    assert node.payload.invocation.capability_id == "memory.read"
    assert node.payload.invocation.kind == "memory"


def test_compile_infers_boundary_for_command_capability() -> None:
    plan = parse_plan_spec_dsl(
        'task: run command\n'
        'run = shell(command="node -e \\"console.log(1);\\"", cwd=".")\n'
    )

    dag = compile_plan_spec(
        plan,
        task_id="task_1",
        tools=[
            CapabilityDefinition(
                id="tool.shell",
                name="shell",
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

    boundary = dag.nodes[0].payload.invocation.boundary
    assert boundary.mode == "write_limited"
    assert boundary.allowed_paths == ["."]
    assert boundary.allowed_commands == []


def test_plan_spec_rejects_node_goal_and_instructions() -> None:
    with pytest.raises(ValueError):
        PlanSpec.model_validate({
            "task": "requirements",
            "nodes": [
                {
                    "id": "write_requirements",
                    "tool": "echo",
                    "args": {"text": "ok"},
                    "goal": "Write a requirement specification.",
                    "instructions": "Use acceptance criteria.",
                }
            ],
        })


def test_compile_plan_spec_preserves_agent_prompt_argument() -> None:
    plan = PlanSpec.model_validate({
        "task": "requirements",
        "nodes": [
            {
                "id": "write_requirements",
                "tool": "helper",
                "args": {"prompt": "Write a requirement specification. Use acceptance criteria."},
            }
        ],
    })

    dag = compile_plan_spec(plan, task_id="task_1", tools=[_capability("helper", "agent.helper", kind="agent")])

    assert dag.nodes[0].payload.invocation.arguments == {
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
    provider = MockProvider(
        [
            ChatResponse(
                content='inspect = shell(command="dir", cwd=".")'
            )
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
    dag = loop.prepare_for_review(requested)

    validate_dag(dag)
    assert dag.task_id == "task_1"
    assert [node.payload.invocation.capability_id for node in dag.nodes] == ["tool.shell"]
    assert [node.payload.invocation.risk for node in dag.nodes] == ["low"]


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


def _capability(name: str, capability_id: str, *, kind: str = "tool") -> CapabilityDefinition:
    return CapabilityDefinition(id=capability_id, name=name, kind=kind)
