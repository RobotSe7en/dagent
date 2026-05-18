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

    result = await runtime.handle_message(
        "Create a low-risk plan that directly answers what dagent is without reading files.",
        mode="dag",
        review_level="careful",
    )
    record = runtime.tasks[result.task_id]

    assert record.dag.task_id == result.task_id
    assert record.dag.nodes
    assert record.dag.status == "review_required"


@pytest.mark.asyncio
async def test_minimax_harness_executes_safe_dag() -> None:
    runtime = create_harness_runtime(workspace_root=".")

    result = await runtime.handle_message(
        "Create and execute a safe plan that answers in one sentence: dagent is a human-reviewed DAG agent framework.",
        mode="dag",
        review_level="fast",
    )
    if result.trace is None:
        assert result.dag is not None
        assert result.pending_review is not None
        resumed = await runtime.resume_review(
            result.pending_review.review_id,
            dag=result.dag,
            review_level="fast",
        )
        assert resumed is not None
        assert resumed.trace is not None
        trace = resumed.trace
    else:
        trace = result.trace

    assert trace.status == "completed"
    assert any(child.kind == "dag_node" for child in trace.root.children)
