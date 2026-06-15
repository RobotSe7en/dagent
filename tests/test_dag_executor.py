import asyncio

import pytest

from dagent.harness_runtime import DAGExecutionError, DAGExecutor
from dagent.harness_runtime.dag_builder import DAGValidationError
from dagent.harness_runtime import CapabilityExecutor
from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.providers import AgentCapabilityProvider, ToolCapabilityProvider
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import Artifact, Boundary, DAG, DAGEdge, DAGNode, DAGSpec, CapabilityInvocation, RunTrace, RunTraceNode
from dagent.capabilities.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def dag_node_trace(trace: RunTrace, node_id: str) -> RunTraceNode:
    for child in trace.root.children:
        if child.kind == "dag_node" and child.ref.get("node_id") == node_id:
            return child
    raise AssertionError(f"Missing dag_node trace for {node_id}")


def capability_trace(trace: RunTrace, node_id: str) -> RunTraceNode:
    node_trace = dag_node_trace(trace, node_id)
    for child in node_trace.children:
        if child.kind == "capability_call":
            return child
    raise AssertionError(f"Missing capability_call trace for {node_id}")


def node_outputs(trace: RunTrace) -> dict[str, str]:
    return {
        child.ref["node_id"]: str(child.output)
        for child in trace.root.children
        if child.kind == "dag_node" and child.ref.get("node_id")
    }


def expr(payload: dict) -> dict:
    return {"$expr": payload}


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
        payload=dict(
            type="capability",
            invocation=CapabilityInvocation(
                capability_id=_tool_capability_id(tool),
                kind="tool",
                arguments=args or {"text": node_id},
                boundary=boundary or Boundary(),
                risk=risk,
            ),
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
            initial_trace=first,
        )
    )

    assert result.status == "completed"
    assert node_outputs(result) == {"a": "echo:a", "b": "echo:b"}
    assert capability_trace(result, "a").capability_execution.result.content == "echo:a"
    assert capability_trace(result, "b").capability_execution.result.content == "echo:b"


def test_executor_skips_trace_snapshot_when_layer_makes_no_progress() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(dag_id="dag_1", task_id="task_1", nodes=[node("a")])
    events: list[dict] = []

    first = run(executor.execute_next_ready_layer(dag, on_event=events.append))
    assert len([event for event in events if event["type"] == "trace"]) == 1

    run(executor.execute_next_ready_layer(dag, initial_trace=first, on_event=events.append))

    assert len([event for event in events if event["type"] == "trace"]) == 1


def test_dag_event_emitter_skips_identical_consecutive_dags() -> None:
    from dagent.harness_runtime.runtime_events import _dag_event_emitter

    events: list[dict] = []
    emit = _dag_event_emitter(events.append)
    dag = DAG(dag_id="dag_1", task_id="task_1", nodes=[node("a")])

    emit(dag)
    emit(dag.model_copy(deep=True))
    completed = dag.model_copy(deep=True)
    completed.status = "completed"
    emit(completed)

    assert len(events) == 2
    assert events[0]["dag"]["status"] != "completed"
    assert events[1]["dag"]["status"] == "completed"


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

    assert result.status == "completed"
    assert str(dag_node_trace(result, "write").output).endswith("notes.md:hi")
    assert dag.nodes[0].payload.invocation.risk == "low"


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

    assert result.status == "completed"


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
        payload=dict(
            type="capability",
            invocation=CapabilityInvocation(
                capability_id=_tool_capability_id(tool),
                kind="tool",
                arguments=args,
                boundary=boundary or Boundary(),
                risk=risk,
            ),
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
        name="shell",
        handler=lambda command, cwd=".", timeout_seconds=30: f"ran:{command}:{cwd}",
        action="command",
        path_args=("cwd",),
        command_args=("command",),
        default_args={"cwd": ".", "timeout_seconds": 30},
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout_seconds": {"type": "integer"},
            },
            "required": ["command"],
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


def test_executor_treats_unapproved_boundary_violation_as_node_failure() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            node(
                "write",
                tools=["write_note"],
                args={"path": "notes.md", "content": "hi"},
                boundary=Boundary(mode="read_only"),
            )
        ],
    )

    with pytest.raises(Exception, match="read_only boundary cannot perform write operations"):
        run(executor.execute_next_ready_layer(dag))

    failed = executor.partial_node_traces["write"]
    assert failed.status == "failed"
    assert failed.children[0].kind == "capability_call"
    assert failed.children[0].status == "failed"


def test_executor_approved_status_does_not_authorize_boundary_override() -> None:
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

    with pytest.raises(DAGExecutionError, match="read_only boundary cannot perform write operations"):
        run(executor.execute_next_ready_layer(dag))


def test_executor_explicit_boundary_authorization_allows_read_only_write_node() -> None:
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

    result = run(executor.execute_next_ready_layer(dag, approve_node_boundaries=True))

    assert result.status == "completed"
    assert dag_node_trace(result, "write").output.endswith("notes.md:hi")


def test_executor_explicit_boundary_authorization_allows_path_outside_node_allowed_paths() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="approved",
        nodes=[
            node(
                "write",
                tools=["write_file"],
                args={"path": "blocked/notes.md", "content": "hi"},
                boundary=Boundary(mode="write_limited", allowed_paths=["allowed"]),
                risk="medium",
            )
        ],
    )

    result = run(executor.execute_next_ready_layer(dag, approve_node_boundaries=True))

    assert result.status == "completed"
    assert dag_node_trace(result, "write").output.endswith("blocked/notes.md:hi")


def test_approved_dag_still_blocks_hard_boundary_command() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="approved",
        nodes=[
            node(
                "danger",
                tools=["shell"],
                args={"command": "rm -rf /"},
                boundary=Boundary(mode="full"),
                risk="high",
            )
        ],
    )

    with pytest.raises(DAGExecutionError, match="shell safety policy"):
        run(executor.execute_next_ready_layer(dag, approve_node_boundaries=True))


def test_executor_embedded_approved_status_does_not_authorize_boundary_override() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    child_spec = DAGSpec(
        id="child",
        name="Child",
        nodes=[
            node(
                "child_write",
                tools=["write_file"],
                args={"path": "notes.md", "content": "hi"},
                boundary=Boundary(mode="read_only"),
            )
        ],
    )
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="draft",
        nodes=[
            DAGNode(
                id="subgraph",
                payload=dict(type="subgraph", spec=child_spec),
            )
        ],
    )

    with pytest.raises(DAGExecutionError, match="read_only boundary cannot perform write operations"):
        run(executor.execute_next_ready_layer(dag))


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

    assert result.status == "completed"
    assert dag_node_trace(result, "echo").output == "echo:hi"
    capability = capability_trace(result, "echo")
    assert capability.capability_execution.invocation.capability_id == "tool.echo"
    assert capability.capability_execution.invocation.arguments == {"text": "hi"}
    assert capability.capability_execution.result.content == "echo:hi"
    assert capability.capability_execution.result.status == "completed"
    assert capability.ref["invocation_id"] == capability.capability_execution.invocation.invocation_id
    assert capability.ref["capability_id"] == "tool.echo"


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
                    content="You are a DAG node agent.",
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
                payload=dict(
                    type="capability",
                    invocation=CapabilityInvocation(
                        capability_id="agent.helper",
                        kind="agent",
                        arguments={"prompt": "Draft requirements. Keep it concise."},
                    ),
                ),
                inputs=["source_doc"],
                outputs=["requirements_doc"],
            )
        ],
    )

    result = run(executor.execute_next_ready_layer(dag))

    assert dag_node_trace(result, "agent_node").output == "agent done"
    messages = provider.requests[0]["messages"]
    system = messages[0]["content"]
    user = messages[1]["content"]
    assert "You are a DAG node agent." in system
    assert str(workspace) in system
    assert str(workspace / "inputs" / "source.md") in system
    assert str(workspace / "outputs" / "requirements.md") in system
    assert "Write requirements" in user
    assert "Draft requirements. Keep it concise." in user
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
                    content="You are a DAG node agent.",
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
                payload=dict(
                    type="capability",
                    invocation=CapabilityInvocation(
                        capability_id="agent.helper",
                        kind="agent",
                        arguments={"prompt": "Use echo."},
                    ),
                ),
            )
        ],
    )
    events: list[dict] = []

    result = run(executor.execute_next_ready_layer(dag, on_event=events.append))

    assert dag_node_trace(result, "agent_node").output == "done"
    capability_events = [event for event in events if event["type"].startswith("capability_")]
    assert capability_events[0]["type"] == "capability_call"
    assert capability_events[0]["capability_id"] == "tool.echo"
    assert capability_events[0]["parent_capability_id"] == "agent.helper"
    assert capability_events[0]["run_id"] == "run_1"
    assert capability_events[0]["dag_id"] == "dag_1"
    assert capability_events[0]["node_id"] == "agent_node"
    assert capability_events[1]["type"] == "capability_result"
    assert capability_events[1]["content"] == "echo:hi"
    response_events = [event for event in events if event["type"].startswith("response_")]
    assert response_events
    for event in response_events:
        assert event["run_id"] == "run_1"
        assert event["dag_id"] == "dag_1"
        assert event["node_id"] == "agent_node"
        assert event["parent_capability_id"] == "agent.helper"


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

    assert first.status == "running"
    assert node_outputs(first) == {"a": "echo:a"}

    second = run(
        executor.execute_next_ready_layer(
            dag,
            initial_trace=first,
        )
    )

    assert second.status == "completed"
    assert node_outputs(second) == {"a": "echo:a", "b": "echo:b"}
    assert [
        child.ref["node_id"]
        for child in second.root.children
        if child.kind == "dag_node"
    ] == ["a", "b"]


def test_executor_resolves_completed_node_value_into_downstream_args() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            tool_node("source", tool="echo", args={"text": "value"}),
            tool_node(
                "sink",
                tool="echo",
                args={
                    "text": expr({
                        "type": "node_output",
                        "node_id": "source",
                        "field": "value",
                        "path": [],
                    })
                },
            ),
        ],
        edges=[DAGEdge(source="source", target="sink")],
    )

    first = run(executor.execute_next_ready_layer(dag))
    result = run(executor.execute_next_ready_layer(dag, initial_trace=first))

    assert result.status == "completed"
    assert dag_node_trace(result, "source").output == "echo:value"
    assert dag_node_trace(result, "sink").output == "echo:echo:value"
    assert capability_trace(result, "sink").capability_execution.invocation.arguments == {"text": "echo:value"}


def test_stepwise_executor_resolves_value_expr_from_initial_results() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            tool_node("source", tool="echo", args={"text": "value"}),
            tool_node(
                "sink",
                tool="echo",
                args={
                    "text": expr({
                        "type": "node_output",
                        "node_id": "source",
                        "field": "content",
                        "path": [],
                    })
                },
            ),
        ],
        edges=[DAGEdge(source="source", target="sink")],
    )

    first = run(executor.execute_next_ready_layer(dag))
    second = run(executor.execute_next_ready_layer(dag, initial_trace=first))

    assert second.status == "completed"
    assert dag_node_trace(second, "sink").output == "echo:echo:value"


def test_executor_rejects_node_value_ref_without_completed_dependency() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            tool_node(
                "sink",
                tool="echo",
                args={
                    "text": expr({
                        "type": "node_output",
                        "node_id": "missing",
                        "field": "value",
                        "path": [],
                    })
                },
            ),
        ],
    )

    with pytest.raises(DAGValidationError, match="missing"):
        run(executor.execute_next_ready_layer(dag))

    assert executor.partial_node_traces == {}


def test_tool_node_failure_marks_node_failed() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    failing_node = tool_node(
        "fragile",
        tool="fail_tool",
        args={"text": "boom"},
    )

    with pytest.raises(DAGExecutionError, match="failed:boom"):
        run(
            executor.execute_next_ready_layer(
                DAG(dag_id="dag_1", task_id="task_1", nodes=[failing_node]),
            )
        )

    assert executor.partial_node_traces["fragile"].status == "failed"
    assert executor.partial_node_traces["fragile"].children[0].status == "failed"


def test_tool_node_boundary_violation_records_failed_node() -> None:
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            tool_node(
                "write_note",
                tool="write_note",
                args={"path": "notes.md", "content": "hi"},
                boundary=Boundary(mode="read_only"),
            )
        ],
    )

    with pytest.raises(Exception, match="read_only boundary cannot perform write operations"):
        run(executor.execute_next_ready_layer(dag))

    failed = executor.partial_node_traces["write_note"]
    capability = failed.children[0]
    assert capability.capability_execution.invocation.capability_id == "tool.write_note"
    assert capability.capability_execution.invocation.arguments == {"path": "notes.md", "content": "hi"}
    assert capability.status == "failed"
    assert capability.error.message
