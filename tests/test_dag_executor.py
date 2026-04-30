import asyncio
import time

import pytest

from dagent.harness_runtime import AgentLoopResult, DAGExecutionError, DAGExecutor, topo_batches
from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode
from dagent.tools.boundary import BoundaryViolation
from dagent.tools.executor import ToolExecutor
from dagent.tools.registry import ToolRegistry


class FakeAgentLoop:
    def __init__(self, delay_seconds: float = 0) -> None:
        self.delay_seconds = delay_seconds
        self.calls: list[dict] = []

    async def run(
        self,
        user_message: str,
        *,
        boundary: Boundary,
        max_steps: int = 8,
        allowed_tools: list[str] | None = None,
        messages: list[dict] | None = None,
    ) -> AgentLoopResult:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        self.calls.append(
            {
                "user_message": user_message,
                "boundary": boundary,
                "max_steps": max_steps,
                "allowed_tools": allowed_tools,
            }
        )
        goal = user_message.splitlines()[0].removeprefix("Node goal: ")
        return AgentLoopResult(
            final_response=f"done:{goal}",
            messages=[],
            steps=1,
            completed=True,
            stop_reason="completed",
        )


class BoundaryBlockingLoop(FakeAgentLoop):
    async def run(
        self,
        user_message: str,
        *,
        boundary: Boundary,
        max_steps: int = 8,
        allowed_tools: list[str] | None = None,
        messages: list[dict] | None = None,
    ) -> AgentLoopResult:
        self.calls.append(
            {
                "user_message": user_message,
                "boundary": boundary,
                "max_steps": max_steps,
                "allowed_tools": allowed_tools,
            }
        )
        raise BoundaryViolation(
            "Write access is not allowed.",
            action="write",
            path="notes.md",
        )


def run(coro):
    return asyncio.run(coro)


def node(
    node_id: str,
    *,
    tools: list[str] | None = None,
    risk: str = "low",
    boundary: Boundary | None = None,
) -> DAGNode:
    return DAGNode(
        id=node_id,
        title=node_id,
        goal=f"goal {node_id}",
        tools=tools or [],
        risk=risk,
        boundary=boundary or Boundary(),
    )


def test_topo_batches_groups_parallel_nodes() -> None:
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[node("a"), node("b"), node("c")],
        edges=[
            DAGEdge(source="a", target="c"),
            DAGEdge(source="b", target="c"),
        ],
    )

    batches = topo_batches(dag)

    assert [[n.id for n in batch] for batch in batches] == [["a", "b"], ["c"]]


def test_executor_runs_ordered_dag_and_records_trace() -> None:
    loop = FakeAgentLoop()
    executor = DAGExecutor(agent_loop=loop)
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[node("a"), node("b")],
        edges=[DAGEdge(source="a", target="b")],
    )

    result = run(executor.execute(dag))

    assert result.completed is True
    assert list(result.node_results) == ["a", "b"]
    assert "done:goal a" in loop.calls[1]["user_message"]
    assert [event.event_type for event in result.traces] == [
        "dag_started",
        "node_started",
        "node_completed",
        "node_started",
        "node_completed",
        "dag_completed",
    ]


def test_executor_runs_independent_nodes_concurrently() -> None:
    loop = FakeAgentLoop(delay_seconds=0.1)
    executor = DAGExecutor(agent_loop=loop)
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[node("a"), node("b")],
        edges=[],
    )

    start = time.perf_counter()
    result = run(executor.execute(dag))
    elapsed = time.perf_counter() - start

    assert result.completed is True
    assert elapsed < 0.18


def test_risk_override_promotes_write_file_to_medium() -> None:
    loop = FakeAgentLoop()
    executor = DAGExecutor(agent_loop=loop)
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="approved",
        nodes=[node("write", tools=["write_file"], risk="low")],
    )

    result = run(executor.execute(dag))

    assert result.completed is True
    assert loop.calls[0]["allowed_tools"] == ["write_file"]
    assert dag.nodes[0].risk == "low"


def test_medium_risk_dag_requires_approval() -> None:
    loop = FakeAgentLoop()
    executor = DAGExecutor(agent_loop=loop)
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="draft",
        nodes=[node("write", tools=["write_file"], risk="low")],
    )

    with pytest.raises(DAGExecutionError, match="not approved"):
        run(executor.execute(dag))
    assert loop.calls == []


def test_high_risk_dag_requires_approval() -> None:
    loop = FakeAgentLoop()
    executor = DAGExecutor(agent_loop=loop)
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="draft",
        nodes=[node("deploy", tools=["deploy"], risk="low")],
    )

    with pytest.raises(DAGExecutionError, match="not approved"):
        run(executor.execute(dag))
    assert loop.calls == []


def test_broad_allowed_paths_promotes_to_medium_and_requires_approval() -> None:
    executor = DAGExecutor(agent_loop=FakeAgentLoop())
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

    with pytest.raises(DAGExecutionError, match="not approved"):
        run(executor.execute(dag))


def test_executor_pauses_when_node_requests_permission() -> None:
    executor = DAGExecutor(agent_loop=BoundaryBlockingLoop())
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        status="approved",
        nodes=[node("write", tools=["write_file"], risk="medium")],
    )


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
    return ToolExecutor(registry)

    result = run(executor.execute(dag))

    assert result.completed is False
    assert result.pending_permission_request is not None
    assert result.pending_permission_request.node_id == "write"
    assert result.pending_permission_request.requested_boundary.mode == "write_limited"
    assert result.pending_permission_request.requested_boundary.allowed_paths == ["notes.md"]
    assert result.node_results["write"].stop_reason == "blocked_permission"
    assert [event.event_type for event in result.traces] == [
        "dag_started",
        "node_started",
        "permission_requested",
        "node_blocked_permission",
        "dag_paused",
    ]


def test_executor_runs_tool_node_directly_without_agent_loop() -> None:
    loop = FakeAgentLoop()
    executor = DAGExecutor(agent_loop=loop, tool_executor=tool_executor())
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

    result = run(executor.execute(dag))

    assert result.completed is True
    assert result.node_results["echo"].final_response == "echo:hi"
    assert loop.calls == []
    assert [event.event_type for event in result.traces] == [
        "dag_started",
        "node_started",
        "tool_called",
        "tool_completed",
        "node_completed",
        "dag_completed",
    ]


def test_tool_node_boundary_violation_pauses_for_permission() -> None:
    loop = FakeAgentLoop()
    executor = DAGExecutor(agent_loop=loop, tool_executor=tool_executor())
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

    result = run(executor.execute(dag))

    assert result.completed is False
    assert result.pending_permission_request is not None
    assert result.pending_permission_request.node_id == "write_note"
    assert result.pending_permission_request.requested_boundary.mode == "write_limited"
    assert result.pending_permission_request.requested_boundary.allowed_paths == ["notes.md"]
    assert loop.calls == []
