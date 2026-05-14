import os

import pytest

from dagent.factory import create_harness_runtime


pytestmark = pytest.mark.skipif(
    os.environ.get("DAGENT_RUN_MINIMAX_TESTS") != "1",
    reason="Set DAGENT_RUN_MINIMAX_TESTS=1 to run real MiniMax integration tests.",
)


@pytest.mark.asyncio
async def test_minimax_dag_agent_generates_valid_dag() -> None:
    runtime = create_harness_runtime(workspace_root=".")

    result = await runtime.dag_agent_loop.run(
        "Create a low-risk plan that directly answers what dagent is without reading files.",
        task_id="minimax_dag_agent_test",
        force_review=True,
    )
    record = runtime.tasks[result.task_id]

    assert record.dag.task_id == "minimax_dag_agent_test"
    assert record.dag.nodes
    assert record.dag.status in {"approved", "review_required"}


@pytest.mark.asyncio
async def test_minimax_harness_executes_safe_dag() -> None:
    runtime = create_harness_runtime(workspace_root=".")

    result = await runtime.dag_agent_loop.run(
        "Create and execute a safe plan that answers in one sentence: dagent is a human-reviewed DAG agent framework.",
        task_id="minimax_execute_test",
        review_level="fast",
    )
    if result.dag_run is None:
        assert result.dag is not None
        assert result.pending_review is not None
        resumed = await runtime.resume_review(
            result.pending_review.review_id,
            dag=result.dag,
            review_level="fast",
        )
        assert resumed is not None
        assert resumed.dag_run is not None
        dag_run = resumed.dag_run
    else:
        dag_run = result.dag_run

    assert dag_run.completed is True
    assert dag_run.node_results
    assert dag_run.traces[0].event_type == "dag_started"
    assert dag_run.traces[-1].event_type == "dag_completed"
