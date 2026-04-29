import os

import pytest

from dagent.factory import create_control_plane


pytestmark = pytest.mark.skipif(
    os.environ.get("DAGENT_RUN_MINIMAX_TESTS") != "1",
    reason="Set DAGENT_RUN_MINIMAX_TESTS=1 to run real MiniMax integration tests.",
)


@pytest.mark.asyncio
async def test_minimax_planner_generates_valid_dag() -> None:
    control_plane = create_control_plane(workspace_root=".")

    record = await control_plane.create_task(
        "生成一个低风险计划：直接回答 dagent 是什么，不需要读取文件。",
        task_id="minimax_planner_test",
    )

    assert record.dag.task_id == "minimax_planner_test"
    assert record.dag.nodes
    assert record.dag.status in {"approved", "review_required"}


@pytest.mark.asyncio
async def test_minimax_harness_executes_safe_dag() -> None:
    control_plane = create_control_plane(workspace_root=".")

    record = await control_plane.create_task(
        "生成并执行一个只需直接回答的计划：用一句话说明 dagent 是 human-reviewed DAG agent framework。",
        task_id="minimax_execute_test",
    )
    if record.dag.status == "review_required":
        control_plane.approve_dag(record.task_id)

    result = await control_plane.execute_task(record.task_id)

    assert result.completed is True
    assert result.node_results
    assert result.traces[0].event_type == "dag_started"
    assert result.traces[-1].event_type == "dag_completed"

