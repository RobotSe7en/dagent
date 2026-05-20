import asyncio

from dagent.capabilities import CapabilityCatalog
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.harness_runtime import CapabilityExecutor, DAGExecutor
from dagent.harness_runtime.tool_agent import ToolAgentLoop
from dagent.capabilities.providers import AgentCapabilityProvider
from dagent.capabilities import CapabilityToolAdapter, CapabilityToolset
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import (
    CapabilityExecution,
    CapabilityInvocation,
    CapabilityResult,
    Boundary,
    DAG,
    DAGNode,
    RunTrace,
    RunTraceNode,
)
from dagent.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def test_run_trace_node_wraps_capability_execution() -> None:
    invocation = CapabilityInvocation(
        invocation_id="call_1",
        capability_id="tool.echo",
        kind="tool",
        arguments={"text": "hi"},
    )
    result = CapabilityResult(
        invocation_id="call_1",
        capability_id="tool.echo",
        kind="tool",
        status="completed",
        content="echo:hi",
    )

    node = RunTraceNode.capability_call(
        parent_id="node_1",
        invocation=invocation,
        result=result,
        output=result.content,
    )

    assert node.kind == "capability_call"
    assert node.status == "completed"
    assert node.capability_execution == CapabilityExecution(
        invocation=invocation,
        result=result,
    )
    assert node.children == []


def test_dag_executor_returns_run_trace_tree_for_ready_layer() -> None:
    executor = DAGExecutor(capability_executor=_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            DAGNode(
                id="echo",
                invocation=CapabilityInvocation(
                    capability_id="tool.echo",
                    kind="tool",
                    arguments={"text": "hi"},
                ),
            )
        ],
    )

    trace = run(executor.execute_next_ready_layer(dag))

    assert isinstance(trace, RunTrace)
    assert trace.root.kind == "run"
    assert trace.root.status == "completed"
    dag_node = trace.root.children[0]
    assert dag_node.kind == "dag_node"
    assert dag_node.ref["node_id"] == "echo"
    capability = dag_node.children[0]
    assert capability.kind == "capability_call"
    assert capability.capability_execution is not None
    assert capability.capability_execution.invocation.capability_id == "tool.echo"
    assert capability.capability_execution.result is not None
    assert capability.capability_execution.result.content == "echo:hi"
    assert capability.output == "echo:hi"


def test_tool_agent_loop_returns_run_trace_for_capability_call() -> None:
    executor = _capability_executor()
    loop = ToolAgentLoop(
        provider=MockProvider([
            ChatResponse(tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})]),
            ChatResponse(content="done"),
        ]),
        capability_executor=executor,
        tool_adapter=_tool_adapter(executor.catalog),
    )

    outcome = run(loop.run("say hi", boundary=Boundary(mode="read_only")))

    assert outcome.trace is not None
    assert outcome.trace.root.kind == "run"
    model_call = outcome.trace.root.children[0]
    capability = outcome.trace.root.children[1]
    assert model_call.kind == "model_call"
    assert capability.kind == "capability_call"
    assert capability.capability_execution is not None
    assert capability.capability_execution.invocation.capability_id == "tool.echo"
    assert capability.capability_execution.result is not None
    assert capability.capability_execution.result.content == "echo:hi"


def test_agent_capability_trace_contains_inner_loop_children(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "inside"})]),
        ChatResponse(content="agent done"),
    ])
    catalog = CapabilityCatalog(workspace_root=tmp_path)
    ToolCapabilityProvider(_echo_registry()).register_into(catalog)
    capability_executor = CapabilityExecutor(catalog)
    AgentCapabilityProvider(
        agents={
            "helper": {
                "provider": provider,
                "profile": AgentProfile(
                    name="helper",
                    role="agent",
                    layers=["agent.md"],
                    layer_contents={"agent.md": "You are a helper."},
                ),
                "capability_executor": capability_executor,
                "tool_adapter": CapabilityToolAdapter(
                    catalog,
                    toolsets=[CapabilityToolset("builtin", ("tool.echo",))],
                ),
                "enabled_toolsets": ("builtin",),
            }
        }
    ).register_into(catalog)
    dag = DAG(
        dag_id="dag_1",
        task_id="run_1",
        nodes=[
            DAGNode(
                id="agent_node",
                invocation=CapabilityInvocation(capability_id="agent.helper", kind="agent"),
            )
        ],
    )

    trace = run(DAGExecutor(capability_executor=capability_executor).execute_next_ready_layer(dag))

    capability = trace.root.children[0].children[0]
    assert capability.kind == "capability_call"
    assert capability.ref["capability_id"] == "agent.helper"
    assert capability.children
    agent_loop = capability.children[0]
    assert agent_loop.kind == "agent_loop"
    assert any(child.kind == "capability_call" for child in agent_loop.children)


def _capability_executor() -> CapabilityExecutor:
    catalog = CapabilityCatalog()
    ToolCapabilityProvider(_echo_registry()).register_into(catalog)
    return CapabilityExecutor(catalog)


def _echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        name="echo",
        handler=lambda text: f"echo:{text}",
        action="read",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    return registry


def _tool_adapter(catalog):
    from dagent.capabilities import CapabilityToolAdapter, CapabilityToolset

    return CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset("builtin", ("tool.echo",))],
    )
