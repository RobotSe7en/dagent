import asyncio
import json

import pytest

from dagent.harness.control_plane import ControlPlane
from dagent.harness.dag_executor import DAGExecutionError, DAGExecutor
from dagent.harness.planner import LLMPlanner
from dagent.providers import ChatResponse, MockProvider
from dagent.runtime import AgentLoopResult
from dagent.schemas import Boundary


class CompletingLoop:
    async def run(
        self,
        user_message: str,
        *,
        boundary: Boundary,
        max_steps: int = 8,
        allowed_tools: list[str] | None = None,
        messages: list[dict] | None = None,
    ) -> AgentLoopResult:
        return AgentLoopResult(
            final_response="node complete",
            messages=[],
            steps=1,
            completed=True,
            stop_reason="completed",
        )


def run(coro):
    return asyncio.run(coro)


def planner_json(*, tools: list[str] | None = None, risk: str = "low") -> str:
    return json.dumps(
        {
            "dag_id": "dag_real",
            "task_id": "will_be_overridden",
            "version": 1,
            "status": "draft",
            "nodes": [
                {
                    "id": "inspect",
                    "title": "Inspect",
                    "goal": "Inspect the request",
                    "agent": None,
                    "tools": tools or [],
                    "skills": [],
                    "boundary": {
                        "mode": "read_only",
                        "allowed_paths": [],
                        "forbidden_tools": [],
                        "allowed_commands": [],
                        "forbidden_commands": [],
                    },
                    "risk": risk,
                    "risk_reason": "Planner estimate.",
                    "expected_output": "Inspection result.",
                    "max_steps": 2,
                    "timeout_seconds": 300,
                }
            ],
            "edges": [],
        }
    )


def test_llm_planner_parses_model_json_into_dag() -> None:
    provider = MockProvider([ChatResponse(content=planner_json())])
    planner = LLMPlanner(provider)

    dag = run(planner.aplan("Plan something", task_id="task_real"))

    assert dag.dag_id == "dag_real"
    assert dag.task_id == "task_real"
    assert dag.nodes[0].id == "inspect"
    assert provider.requests[0]["messages"][0]["role"] == "system"


def test_control_plane_auto_approves_low_risk_dag_and_executes() -> None:
    provider = MockProvider([ChatResponse(content=planner_json())])
    planner = LLMPlanner(provider)
    executor = DAGExecutor(agent_loop=CompletingLoop())
    control_plane = ControlPlane(planner=planner, executor=executor)

    record = run(control_plane.create_task("Do a safe task", task_id="task_1"))
    result = run(control_plane.execute_task(record.task_id))

    assert record.dag.status == "approved"
    assert result.completed is True
    assert [event.event_type for event in result.traces] == [
        "dag_started",
        "node_started",
        "node_completed",
        "dag_completed",
    ]


def test_control_plane_requires_review_after_risk_override() -> None:
    provider = MockProvider([ChatResponse(content=planner_json(tools=["write_file"]))])
    planner = LLMPlanner(provider)
    executor = DAGExecutor(agent_loop=CompletingLoop())
    control_plane = ControlPlane(planner=planner, executor=executor)

    record = run(control_plane.create_task("Modify a file", task_id="task_1"))

    assert record.dag.status == "review_required"
    assert record.dag.nodes[0].risk == "medium"
    with pytest.raises(DAGExecutionError):
        run(control_plane.execute_task(record.task_id))

    control_plane.approve_dag(record.task_id)
    result = run(control_plane.execute_task(record.task_id))
    assert result.completed is True

