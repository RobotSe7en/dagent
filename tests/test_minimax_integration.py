import os

import pytest

from dagent import DagAgent, Runner


pytestmark = pytest.mark.skipif(
    os.environ.get("DAGENT_RUN_MINIMAX_TESTS") != "1",
    reason="Set DAGENT_RUN_MINIMAX_TESTS=1 to run real MiniMax integration tests.",
)


@pytest.mark.asyncio
async def test_minimax_dag_agent_generates_valid_dag() -> None:
    runner = Runner.from_config(workspace=".")
    agent = DagAgent(review="careful")

    result = await runner.run(
        agent,
        messages=[
            {
                "role": "user",
                "content": "Create a low-risk plan that directly answers what dagent is without reading files.",
            }
        ],
    )
    record = runner.runtime.runs[result.run_id]

    assert record.dag.task_id == result.run_id
    assert record.dag.nodes
    assert record.dag.status == "review_required"


@pytest.mark.asyncio
async def test_minimax_harness_executes_safe_dag() -> None:
    runner = Runner.from_config(workspace=".")
    agent = DagAgent(review="fast")

    result = await runner.run(
        agent,
        messages=[
            {
                "role": "user",
                "content": "Create and execute a safe plan that answers in one sentence: dagent is a human-reviewed DAG agent framework.",
            }
        ],
    )
    if result.trace is None:
        assert result.dag is not None
        assert result.review is not None
        resumed = await runner.resume(result.review.approve(review_level="fast"))
        assert resumed is not None
        assert resumed.trace is not None
        trace = resumed.trace
    else:
        trace = result.trace

    assert trace.status == "completed"
    assert any(child.kind == "dag_node" for child in trace.root.children)
