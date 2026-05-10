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
        "生成一个低风险计划：直接回答 dagent 是什么，不需要读取文件。",
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
        "生成并执行一个只需直接回答的计划：用一句话说明 dagent 是 human-reviewed DAG agent framework。",
        task_id="minimax_execute_test",
        review_level="fast",
    )
    if result.run_result is None:
        assert result.dag is not None
        resumed = await runtime.dag_agent_loop.resume(result.task_id, result.dag, review_level="fast")
        assert resumed.run_result is not None
        run_result = resumed.run_result
    else:
        run_result = result.run_result

    assert run_result.completed is True
    assert run_result.node_results
    assert run_result.traces[0].event_type == "dag_started"
    assert run_result.traces[-1].event_type == "dag_completed"

