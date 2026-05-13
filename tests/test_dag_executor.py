import asyncio

import pytest

from dagent.harness_runtime import DAGExecutionError, DAGExecutor
from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode
from dagent.tools.executor import ToolExecutor
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
        title=node_id,
        goal=f"goal {node_id}",
        kind="tool",
        tool=tool,
        args=args or {"text": node_id},
        tools=[tool],
        risk=risk,
        boundary=boundary or Boundary(),
    )


def test_executor_runs_ordered_dag_and_records_trace() -> None:
    executor = DAGExecutor(tool_executor=tool_executor())
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
        "tool_called",
        "tool_completed",
        "node_completed",
        "node_started",
        "tool_called",
        "tool_completed",
        "node_completed",
        "dag_completed",
    ]


def test_risk_override_promotes_write_file_to_medium() -> None:
    executor = DAGExecutor(tool_executor=tool_executor())
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
    assert dag.nodes[0].risk == "low"


def test_medium_risk_dag_requires_approval() -> None:
    executor = DAGExecutor(tool_executor=tool_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="draft",
        nodes=[node("write", tools=["write_file"], risk="medium")],
    )

    with pytest.raises(DAGExecutionError, match="not approved"):
        run(executor.execute_next_ready_layer(dag))


def test_high_risk_dag_requires_approval() -> None:
    executor = DAGExecutor(tool_executor=tool_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="draft",
        nodes=[node("write", tools=["write_file"], risk="high")],
    )

    with pytest.raises(DAGExecutionError, match="not approved"):
        run(executor.execute_next_ready_layer(dag))


def test_read_only_broad_paths_does_not_require_approval() -> None:
    executor = DAGExecutor(tool_executor=tool_executor())
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
        title=node_id,
        goal=f"run {tool}",
        kind="tool",
        tool=tool,
        args=args,
        risk=risk,
        boundary=boundary or Boundary(),
    )


def tool_executor() -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(
        name="dag_start",
        handler=lambda: "started",
        action="read",
        parameters={
            "type": "object",
            "properties": {},
        },
    )
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
    return ToolExecutor(registry)


def test_executor_treats_boundary_violation_as_node_failure() -> None:
    executor = DAGExecutor(tool_executor=tool_executor())
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
        "tool_called",
        "tool_failed",
        "node_failed",
        "dag_failed",
    ]


def test_executor_runs_tool_node_directly_without_tool_agent_loop() -> None:
    executor = DAGExecutor(tool_executor=tool_executor())
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
    records = executor.trace_store.records_for_task("task_1")
    assert len(records) == 1
    assert records[0].node_id == "echo"
    assert records[0].tool == "echo"
    assert records[0].args == {"text": "hi"}
    assert records[0].output == "echo:hi"
    assert records[0].status == "completed"
    assert [event.event_type for event in result.traces] == [
        "dag_started",
        "node_started",
        "tool_called",
        "tool_completed",
        "node_completed",
        "dag_completed",
    ]


def test_executor_can_run_one_ready_layer_at_a_time() -> None:
    executor = DAGExecutor(tool_executor=tool_executor())
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
        "tool_called",
        "tool_completed",
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
    assert [record.node_id for record in executor.trace_store.records_for_task("task_1")] == ["a", "b"]


def test_executor_injects_completed_node_output_into_downstream_args() -> None:
    executor = DAGExecutor(tool_executor=tool_executor())
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
    records = executor.trace_store.records_for_task("task_1")
    assert records[1].node_id == "sink"
    assert records[1].args == {"text": "echo:value"}


def test_stepwise_executor_injects_placeholders_from_initial_results() -> None:
    executor = DAGExecutor(tool_executor=tool_executor())
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
    executor = DAGExecutor(tool_executor=tool_executor())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[
            tool_node("sink", tool="echo", args={"text": "{{missing.output}}"}),
        ],
    )

    with pytest.raises(DAGExecutionError, match="missing"):
        run(executor.execute_next_ready_layer(dag))

    assert executor.trace_store.records_for_task("task_1") == []


def test_tool_node_failure_marks_node_failed() -> None:
    executor = DAGExecutor(tool_executor=tool_executor())
    failing_node = tool_node(
        "fragile",
        tool="fail_tool",
        args={"text": "boom"},
    )

    with pytest.raises(RuntimeError, match="failed:boom"):
        executor.execute_tool_node(
            failing_node,
            DAG(dag_id="dag_1", task_id="task_1", nodes=[failing_node]),
        )

    assert failing_node.status == "failed"
    records = executor.trace_store.records_for_task("task_1")
    assert records[-1].node_id == "fragile"
    assert records[-1].status == "failed"


def test_tool_node_boundary_violation_records_failed_node() -> None:
    executor = DAGExecutor(tool_executor=tool_executor())
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

    records = executor.trace_store.records_for_task("task_1")
    assert len(records) == 1
    assert records[0].node_id == "write_note"
    assert records[0].tool == "write_note"
    assert records[0].args == {"path": "notes.md", "content": "hi"}
    assert records[0].status == "failed"
    assert records[0].stop_reason == "boundary_violation"
    assert records[0].error
