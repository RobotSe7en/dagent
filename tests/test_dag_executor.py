import asyncio
import time

import pytest

from dagent.harness_runtime import AgentLoopResult, DAGExecutionError, DAGExecutor, topo_batches
from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode


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
