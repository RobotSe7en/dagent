import asyncio

import pytest

from dagent.harness_runtime import DAGExecutionError, DAGExecutor
from dagent.harness_runtime import CapabilityExecutor
from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.providers import AgentCapabilityProvider, ToolCapabilityProvider
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import Artifact, Boundary, DAG, DAGEdge, DAGNode, CapabilityInvocation
from dagent.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def node(
    node_id: str,
    *,
    tools: list[str] | None = None,
    risk: str = "low",
    boundary: Boundary | None = None,
    args: dict | None = None,
) -> DAGNode:
    tool = (tools or ["echo"])[0]
    return DAGNode(
        id=node_id,
        invocation=CapabilityInvocation(
            capability_id=_tool_capability_id(tool),
            kind="tool",
            arguments=args or {"text": node_id},
            boundary=boundary or Boundary(),
            risk=risk,
        ),
    )


def test_executor_runs_ordered_dag_and_records_trace() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[node("a"), node("b")],
        edges=[DAGEdge(source="a", target="b")],
    )

    first = run(executor.execute_next_ready_layer(dag))
    result = run(
        executor.execute_next_ready_layer(
            dag,
            initial_results=first.node_results,
            record_dag_start=False,
        )
    )

    assert result.completed is True
    assert list(result.node_results) == ["a", "b"]
    assert result.node_results["a"].final_response == "echo:a"
    assert result.node_results["b"].final_response == "echo:b"
    assert [event.event_type for event in [*first.traces, *result.traces]] == [
        "dag_started",
        "node_started",
        "capability_called",
        "capability_completed",
        "node_completed",
        "node_started",
        "capability_called",
        "capability_completed",
        "node_completed",
        "dag_completed",
    ]


def test_risk_override_promotes_write_file_to_medium() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="approved",
        nodes=[
            node(
                "write",
                tools=["write_file"],
                args={"path": "notes.md", "content": "hi"},
                boundary=Boundary(mode="write_limited", allowed_paths=["notes.md"]),
                risk="low",
            )
        ],
    )

    result = run(executor.execute_next_ready_layer(dag))

    assert result.completed is True
    assert result.node_results["write"].final_response.endswith("notes.md:hi")
    assert dag.nodes[0].invocation.risk == "low"


def test_medium_risk_dag_requires_approval() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="draft",
        nodes=[node("write", tools=["write_file"], risk="medium")],
    )

    with pytest.raises(DAGExecutionError, match="not approved"):
        run(executor.execute_next_ready_layer(dag))


def test_high_risk_dag_requires_approval() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="draft",
        nodes=[node("write", tools=["write_file"], risk="high")],
    )

    with pytest.raises(DAGExecutionError, match="not approved"):
        run(executor.execute_next_ready_layer(dag))


def test_read_only_broad_paths_does_not_require_approval() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="draft",
        nodes=[
            node(
                "broad",
                risk="low",
                boundary=Boundary(mode="read_only", allowed_paths=["."]),
            )
        ],
    )

    result = run(executor.execute_next_ready_layer(dag))

    assert result.completed is True


def tool_node(
    node_id: str,
    *,
    tool: str,
    args: dict,
    boundary: Boundary | None = None,
    risk: str = "low",
) -> DAGNode:
    return DAGNode(
        id=node_id,
        invocation=CapabilityInvocation(
            capability_id=_tool_capability_id(tool),
            kind="tool",
            arguments=args,
            boundary=boundary or Boundary(),
            risk=risk,
        ),
    )


def make_capability_executor() -> CapabilityExecutor:
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
    registry.register(
        name="write_note",
        handler=lambda path, content: f"wrote:{path}:{content}",
        action="write",
        path_args=("path",),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    )
    registry.register(
        name="write_file",
        handler=lambda path, content="": f"wrote:{path}:{content}",
        action="write",
        path_args=("path",),
        risk="medium",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path"],
        },
    )
    registry.register(
        name="fail_tool",
        handler=lambda text: (_ for _ in ()).throw(RuntimeError(f"failed:{text}")),
        action="read",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    capability_catalog = CapabilityCatalog()
    capability_executor = CapabilityExecutor(capability_catalog)
    ToolCapabilityProvider(registry).register_into(capability_catalog)
    return capability_executor


def _tool_capability_id(tool_name: str) -> str:
    return tool_name if tool_name.startswith("tool.") else f"tool.{tool_name}"


def test_executor_treats_boundary_violation_as_node_failure() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="approved",
        nodes=[
            node(
                "write",
                tools=["write_file"],
                args={"path": "notes.md", "content": "hi"},
                boundary=Boundary(mode="read_only"),
                risk="medium",
            )
        ],
    )

    with pytest.raises(Exception, match="read_only boundary cannot perform write operations"):
        run(executor.execute_next_ready_layer(dag))

    assert [event.event_type for event in executor.trace_recorder.events] == [
        "dag_started",
        "node_started",
        "capability_called",
        "capability_failed",
        "node_failed",
        "dag_failed",
    ]


def test_executor_runs_tool_node_directly_without_tool_agent_loop() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            tool_node(
                "echo",
                tool="echo",
                args={"text": "hi"},
            )
        ],
    )

    result = run(executor.execute_next_ready_layer(dag))

    assert result.completed is True
    assert result.node_results["echo"].final_response == "echo:hi"
    records = executor.execution_store.records_for_task("task_1")
    assert len(records) == 1
    assert records[0].node_id == "echo"
    assert records[0].invocation.capability_id == "tool.echo"
    assert records[0].invocation.arguments == {"text": "hi"}
    assert records[0].output == "echo:hi"
    assert records[0].status == "completed"
    assert [event.event_type for event in result.traces] == [
        "dag_started",
        "node_started",
        "capability_called",
        "capability_completed",
        "node_completed",
        "dag_completed",
    ]
    capability_events = [
        event for event in result.traces
        if event.event_type in {"capability_called", "capability_completed"}
    ]
    assert capability_events[0].payload["invocation_id"] == records[0].invocation.invocation_id
    assert capability_events[0].payload["capability_id"] == "tool.echo"
    assert "tool_call_id" not in capability_events[0].payload


def test_executor_passes_node_context_to_agent_capability(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="agent done")])
    catalog = CapabilityCatalog(workspace_root=tmp_path)
    capability_executor = CapabilityExecutor(catalog)
    tool_adapter = CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset("builtin", ())],
    )
    AgentCapabilityProvider(
        agents={
            "helper": {
                "provider": provider,
                "profile": AgentProfile(
                    name="helper",
                    role="agent",
                    layers=["agent.md"],
                    layer_contents={"agent.md": "You are a DAG node agent."},
                ),
                "capability_executor": capability_executor,
                "tool_adapter": tool_adapter,
                "enabled_toolsets": ("builtin",),
            }
        }
    ).register_into(catalog)
    workspace = tmp_path / "run"
    workspace.mkdir()
    executor = DAGExecutor(
        capability_executor=capability_executor,
        workspace_path=workspace,
        artifacts={
            "source_doc": Artifact(id="source_doc", paths=["inputs/source.md"]),
            "requirements_doc": Artifact(id="requirements_doc", paths=["outputs/requirements.md"]),
        },
    )
    dag = DAG(
        dag_id="dag_1",
        task_id="run_1",
        nodes=[
            DAGNode(
                id="agent_node",
                title="Write requirements",
                goal="Draft requirements.",
                instructions="Keep it concise.",
                invocation=CapabilityInvocation(capability_id="agent.helper", kind="agent"),
                inputs=["source_doc"],
                outputs=["requirements_doc"],
            )
        ],
    )

    result = run(executor.execute_next_ready_layer(dag))

    assert result.node_results["agent_node"].final_response == "agent done"
    messages = provider.requests[0]["messages"]
    system = messages[0]["content"]
    user = messages[1]["content"]
    assert "You are a DAG node agent." in system
    assert str(workspace) in system
    assert str(workspace / "inputs" / "source.md") in system
    assert str(workspace / "outputs" / "requirements.md") in system
    assert "Write requirements" in user
    assert "Draft requirements." in user
    assert "Keep it concise." in user
    assert "source_doc" not in user


def test_executor_tags_agent_inner_tool_events_with_node_context(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})]),
        ChatResponse(content="done"),
    ])
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
    catalog = CapabilityCatalog(workspace_root=tmp_path)
    ToolCapabilityProvider(registry).register_into(catalog)
    capability_executor = CapabilityExecutor(catalog)
    tool_adapter = CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset("builtin", ("tool.echo",))],
    )
    AgentCapabilityProvider(
        agents={
            "helper": {
                "provider": provider,
                "profile": AgentProfile(
                    name="helper",
                    role="agent",
                    layers=["agent.md"],
                    layer_contents={"agent.md": "You are a DAG node agent."},
                ),
                "capability_executor": capability_executor,
                "tool_adapter": tool_adapter,
                "enabled_toolsets": ("builtin",),
            }
        }
    ).register_into(catalog)
    executor = DAGExecutor(
        capability_executor=capability_executor,
        workspace_path=tmp_path,
    )
    dag = DAG(
        dag_id="dag_1",
        task_id="run_1",
        nodes=[
            DAGNode(
                id="agent_node",
                title="Call echo",
                goal="Use echo.",
                invocation=CapabilityInvocation(capability_id="agent.helper", kind="agent"),
            )
        ],
    )
    events: list[dict] = []

    result = run(executor.execute_next_ready_layer(dag, on_event=events.append))

    assert result.node_results["agent_node"].final_response == "done"
    assert events[0]["type"] == "capability_call"
    assert events[0]["capability_id"] == "tool.echo"
    assert events[0]["parent_capability_id"] == "agent.helper"
    assert events[0]["task_id"] == "run_1"
    assert events[0]["dag_id"] == "dag_1"
    assert events[0]["node_id"] == "agent_node"
    assert events[1]["type"] == "capability_result"
    assert events[1]["content"] == "echo:hi"


def test_executor_can_run_one_ready_layer_at_a_time() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            tool_node("a", tool="echo", args={"text": "a"}),
            tool_node("b", tool="echo", args={"text": "b"}),
        ],
        edges=[DAGEdge(source="a", target="b")],
    )

    first = run(executor.execute_next_ready_layer(dag))

    assert first.completed is False
    assert list(first.node_results) == ["a"]
    assert [event.event_type for event in first.traces] == [
        "dag_started",
        "node_started",
        "capability_called",
        "capability_completed",
        "node_completed",
    ]

    second = run(
        executor.execute_next_ready_layer(
            dag,
            initial_results=first.node_results,
        )
    )

    assert second.completed is True
    assert list(second.node_results) == ["a", "b"]
    assert [record.node_id for record in executor.execution_store.records_for_task("task_1")] == ["a", "b"]


def test_executor_injects_completed_node_output_into_downstream_args() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            tool_node("source", tool="echo", args={"text": "value"}),
            tool_node("sink", tool="echo", args={"text": "{{source.output}}"}),
        ],
        edges=[DAGEdge(source="source", target="sink")],
    )

    first = run(executor.execute_next_ready_layer(dag))
    result = run(executor.execute_next_ready_layer(dag, initial_results=first.node_results))

    assert result.completed is True
    assert result.node_results["source"].final_response == "echo:value"
    assert result.node_results["sink"].final_response == "echo:echo:value"
    records = executor.execution_store.records_for_task("task_1")
    assert records[1].node_id == "sink"
    assert records[1].invocation.arguments == {"text": "echo:value"}


def test_stepwise_executor_injects_placeholders_from_initial_results() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            tool_node("source", tool="echo", args={"text": "value"}),
            tool_node("sink", tool="echo", args={"text": "{{source.output}}"}),
        ],
        edges=[DAGEdge(source="source", target="sink")],
    )

    first = run(executor.execute_next_ready_layer(dag))
    second = run(executor.execute_next_ready_layer(dag, initial_results=first.node_results))

    assert second.completed is True
    assert second.node_results["sink"].final_response == "echo:echo:value"


def test_executor_rejects_unresolved_placeholders_before_tool_call() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            tool_node("sink", tool="echo", args={"text": "{{missing.output}}"}),
        ],
    )

    with pytest.raises(DAGExecutionError, match="missing"):
        run(executor.execute_next_ready_layer(dag))

    assert executor.execution_store.records_for_task("task_1") == []


def test_tool_node_failure_marks_node_failed() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    failing_node = tool_node(
        "fragile",
        tool="fail_tool",
        args={"text": "boom"},
    )

    with pytest.raises(DAGExecutionError, match="failed:boom"):
        run(
            executor.execute_capability_node(
                failing_node,
                DAG(dag_id="dag_1", task_id="task_1", nodes=[failing_node]),
            )
        )

    assert failing_node.status == "failed"
    records = executor.execution_store.records_for_task("task_1")
    assert records[-1].node_id == "fragile"
    assert records[-1].status == "failed"


def test_tool_node_boundary_violation_records_failed_node() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="approved",
        nodes=[
            tool_node(
                "write_note",
                tool="write_note",
                args={"path": "notes.md", "content": "hi"},
                boundary=Boundary(mode="read_only"),
                risk="medium",
            )
        ],
    )

    with pytest.raises(Exception, match="read_only boundary cannot perform write operations"):
        run(executor.execute_next_ready_layer(dag))

    records = executor.execution_store.records_for_task("task_1")
    assert len(records) == 1
    assert records[0].node_id == "write_note"
    assert records[0].invocation.capability_id == "tool.write_note"
    assert records[0].invocation.arguments == {"path": "notes.md", "content": "hi"}
    assert records[0].status == "failed"
    assert records[0].stop_reason == "boundary_violation"
    assert records[0].error
