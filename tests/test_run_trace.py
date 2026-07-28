import asyncio

from dagent.capabilities import CapabilityCatalog
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.harness_runtime import CapabilityExecutor, DAGExecutor
from dagent.harness_runtime.tool_agent import ToolAgentLoop
from dagent.harness_runtime.context import ContextAssembler
from dagent.capabilities.providers import AgentCapabilityProvider
from dagent.capabilities import CapabilityToolAdapter, CapabilityToolset
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import (
    CapabilityExecution,
    CapabilityInvocation,
    CapabilityResult,
    Boundary,
    ContextPolicy,
    ConversationState,
    DAG,
    DAGNode,
    RunTrace,
    RunTraceNode,
    ResultStoragePolicy,
    UserMessage,
)
from dagent.capabilities.tools.file_tools import create_file_tool_registry
from dagent.capabilities.tools.registry import ToolRegistry


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
    )

    assert node.kind == "capability_call"
    assert node.status == "completed"
    assert node.capability_execution == CapabilityExecution(
        invocation=invocation,
        result=result,
    )
    assert node.input == {}
    assert node.output is None
    assert node.value is None
    assert node.children == []


def test_dag_executor_returns_run_trace_tree_for_ready_layer() -> None:
    executor = DAGExecutor(runtime_directory=".runtime", capability_executor=_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            DAGNode(
                id="echo",
                payload=dict(
                    type="capability",
                    invocation=CapabilityInvocation(
                        capability_id="tool.echo",
                        kind="tool",
                        arguments={"text": "hi"},
                    ),
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


def test_tool_agent_loop_returns_run_trace_for_capability_call() -> None:
    executor = _capability_executor()
    loop = ToolAgentLoop(runtime_directory=".runtime",
        provider=MockProvider([
            ChatResponse(tool_calls=[ToolCall(id="call_1", name="tool_echo", arguments={"text": "hi"})]),
            ChatResponse(content="done"),
        ]),
        capability_executor=executor,
        tool_adapter=_tool_adapter(executor.catalog),
    )

    outcome = run(
        loop.run(
            ConversationState(items=(UserMessage(content="say hi"),)),
            boundary=Boundary(),
            system_message={"role": "system", "content": "Be useful."},
            context_policy=ContextPolicy(),
            result_storage_policy=ResultStoragePolicy(),
            context_assembler=ContextAssembler(),
        )
    )

    assert outcome.state.trace is not None
    assert outcome.state.trace.root.kind == "run"
    model_call = outcome.state.trace.root.children[0]
    capability = outcome.state.trace.root.children[1]
    assert model_call.kind == "model_call"
    assert capability.kind == "capability_call"
    assert capability.capability_execution is not None
    assert capability.capability_execution.invocation.capability_id == "tool.echo"
    assert capability.capability_execution.result is not None
    assert capability.capability_execution.result.content == "echo:hi"


def test_agent_capability_trace_contains_inner_loop_children(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_1", name="tool_echo", arguments={"text": "inside"})]),
        ChatResponse(content="agent done"),
    ])
    catalog = CapabilityCatalog(workspace_root=tmp_path)
    ToolCapabilityProvider(_echo_registry()).register_into(catalog)
    capability_executor = CapabilityExecutor(catalog)
    AgentCapabilityProvider(runtime_directory=".runtime",
        agents={
            "helper": {
                "provider": provider,
                "profile": AgentProfile(
                    name="helper",
                    content="You are a helper.",
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
                payload=dict(
                    type="capability",
                    invocation=CapabilityInvocation(capability_id="agent.helper", kind="agent"),
                ),
            )
        ],
    )

    trace = run(DAGExecutor(runtime_directory=".runtime", capability_executor=capability_executor).execute_next_ready_layer(dag))

    capability = trace.root.children[0].children[0]
    assert capability.kind == "capability_call"
    assert capability.ref["capability_id"] == "agent.helper"
    assert capability.children
    agent_loop = capability.children[0]
    assert agent_loop.kind == "agent_loop"
    assert any(child.kind == "capability_call" for child in agent_loop.children)


def test_agent_capability_inherits_parent_workspace_without_rewriting_boundary(tmp_path) -> None:
    run_workspace = tmp_path / "runs" / "run_1"
    run_workspace.mkdir(parents=True)
    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(
                id="call_1",
                name="tool_write_file",
                arguments={"path": "notes.txt", "content": "from helper"},
            )
        ]),
        ChatResponse(content="agent done"),
    ])
    catalog = CapabilityCatalog(workspace_root=tmp_path)
    ToolCapabilityProvider(create_file_tool_registry()).register_into(catalog)
    capability_executor = CapabilityExecutor(catalog)
    AgentCapabilityProvider(runtime_directory=".runtime",
        agents={
            "helper": {
                "provider": provider,
                "profile": AgentProfile(
                    name="helper",
                    content="You are a helper.",
                ),
                "capability_executor": capability_executor,
                "tool_adapter": CapabilityToolAdapter(
                    catalog,
                    toolsets=[CapabilityToolset("builtin", ("tool.write_file",))],
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
                payload=dict(
                    type="capability",
                    invocation=CapabilityInvocation(capability_id="agent.helper", kind="agent"),
                ),
            )
        ],
    )

    trace = run(DAGExecutor(runtime_directory=".runtime",
        capability_executor=capability_executor,
        workspace_path=run_workspace,
        capability_workspace_root=tmp_path,
    ).execute_next_ready_layer(dag))

    assert (run_workspace / "notes.txt").read_text(encoding="utf-8") == "from helper"
    assert not (tmp_path / "notes.txt").exists()
    tool_invocation = _capability_invocation(trace, "tool.write_file")
    assert str(run_workspace) not in (tool_invocation.boundary.allowed_paths or [])


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


def _capability_invocation(trace: RunTrace, capability_id: str) -> CapabilityInvocation:
    stack = [trace.root]
    while stack:
        node = stack.pop()
        if (
            node.kind == "capability_call"
            and node.capability_execution is not None
            and node.capability_execution.invocation.capability_id == capability_id
        ):
            return node.capability_execution.invocation
        stack.extend(reversed(node.children))
    raise AssertionError(f"Missing capability invocation for {capability_id}")
