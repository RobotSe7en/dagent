import asyncio

import pytest

from dagent.harness_runtime import DAGAgent, DAGAgentLoop, DAGExecutor
from dagent.harness_runtime.dag_builder import DAGValidationError, validate_dag
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import DAG, DAGEdge, DAGNode, RunnableInvocation
from dagent.tools.executor import ToolExecutor
from dagent.tools.registry import ToolRegistry


def make_node(node_id: str) -> DAGNode:
    return DAGNode(
        id=node_id,
        invocation=RunnableInvocation(
            runnable_id="tool.echo",
            kind="tool",
            arguments={"text": node_id},
        ),
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
                invocation=RunnableInvocation(runnable_id="", kind="tool"),
            )
        ],
    )

    with pytest.raises(DAGValidationError, match="must declare a runnable"):
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
                content='inspect = run_command(command="dir", cwd=".")'
            )
        ]
    )
    registry = ToolRegistry()
    registry.register(
        name="run_command",
        handler=lambda command, cwd=".": f"ran:{command}",
        action="read",
        parameters={"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}}},
    )
    tool_executor = ToolExecutor(registry)
    loop = DAGAgentLoop(
        provider=provider,
        dag_executor=DAGExecutor(tool_executor=tool_executor),
    )
    agent = DAGAgent(
        loop=loop,
        profile=_dag_agent_profile(),
        tools=tool_executor.registry.all_tools(),
    )

    messages = [agent.system_message]
    requested = asyncio.run(loop._request_dag(
        task_id="task_1",
        messages=messages,
        user_message=agent.build_request_user_message(
            prompt="Summarize the repo",
            task_id="task_1",
        ),
        tools=agent.tools,
        allow_no_change=False,
    ))
    dag = loop.prepare_for_review(requested)

    validate_dag(dag)
    assert dag.task_id == "task_1"
    assert [node.invocation.runnable_id for node in dag.nodes] == ["tool.run_command"]
    assert [node.invocation.risk for node in dag.nodes] == ["low"]


def _dag_agent_profile() -> AgentProfile:
    return AgentProfile(
        name="dag_agent",
        role="dag_agent",
        layers=["soul"],
        layer_contents={"soul": "You are a DAG creator."},
    )
