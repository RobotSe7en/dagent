import json

from fastapi.testclient import TestClient

from dagent.api.app import app, state
from dagent.harness.control_plane import ControlPlane
from dagent.harness.dag_executor import DAGExecutor
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
            final_response="done",
            messages=[],
            steps=1,
            completed=True,
            stop_reason="completed",
        )


def test_api_creates_approves_and_executes_dag() -> None:
    provider = MockProvider([ChatResponse(content=_planner_json())])
    state.control_plane = ControlPlane(
        planner=LLMPlanner(provider),
        executor=DAGExecutor(agent_loop=CompletingLoop()),
    )
    state.runs.clear()
    client = TestClient(app)

    task_response = client.post(
        "/tasks",
        json={"message": "make a small DAG", "task_id": "task_api"},
    )
    assert task_response.status_code == 200
    task_payload = task_response.json()
    assert task_payload["dag"]["task_id"] == "task_api"

    approve_response = client.post("/dags/task_api/approve")
    assert approve_response.status_code == 200
    assert approve_response.json()["dag"]["status"] == "approved"

    execute_response = client.post("/dags/task_api/execute")
    assert execute_response.status_code == 200
    execute_payload = execute_response.json()
    assert execute_payload["dag"]["status"] == "completed"
    assert execute_payload["result"]["completed"] is True
    assert [event["event_type"] for event in execute_payload["result"]["traces"]] == [
        "dag_started",
        "node_started",
        "node_completed",
        "dag_completed",
    ]


def _planner_json() -> str:
    return json.dumps(
        {
            "dag_id": "dag_api",
            "task_id": "ignored",
            "version": 1,
            "status": "draft",
            "nodes": [
                {
                    "id": "answer",
                    "title": "Answer",
                    "goal": "Answer the user.",
                    "agent": None,
                    "tools": [],
                    "skills": [],
                    "boundary": {
                        "mode": "read_only",
                        "allowed_paths": [],
                        "forbidden_tools": [],
                        "allowed_commands": [],
                        "forbidden_commands": [],
                    },
                    "risk": "low",
                    "risk_reason": "No tool access.",
                    "expected_output": "Answer.",
                    "max_steps": 1,
                    "timeout_seconds": 30,
                }
            ],
            "edges": [],
        }
    )
