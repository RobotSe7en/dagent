import json

from fastapi.testclient import TestClient

from dagent.api.app import app, state
from dagent.harness_runtime import (
    AgentLoop,
    AgentLoopResult,
    ControlPlane,
    DAGExecutor,
    HarnessRuntime,
    LLMDagCreator,
)
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import Boundary
from dagent.tools.executor import ToolExecutor
from dagent.tools.registry import ToolRegistry


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
    provider = MockProvider([ChatResponse(content=_dag_creator_json())])
    state.control_plane = ControlPlane(
        dag_creator=LLMDagCreator(provider),
        executor=DAGExecutor(agent_loop=CompletingLoop()),
    )
    state.harness_runtime = None
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


def test_api_approved_medium_risk_runtime_dag_executes() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                content=_dag_creator_json(
                    tools=["write_file"],
                    boundary_mode="write_limited",
                )
            )
        ]
    )
    state.harness_runtime = HarnessRuntime(
        agent_loop=AgentLoop(
            provider=MockProvider([ChatResponse(content="unused")]),
            tool_executor=ToolExecutor(ToolRegistry()),
        ),
        dag_creator=LLMDagCreator(provider, profile=_profile("dag_creator")),
        dag_executor=DAGExecutor(agent_loop=CompletingLoop()),
        conversation_profile=_profile("conversation"),
    )
    state.control_plane = None
    state.runs.clear()
    client = TestClient(app)

    create_response = client.post(
        "/messages/stream",
        json={"message": "create risky dag", "mode": "dag_creator"},
    )
    assert create_response.status_code == 200
    assert '"status": "review_required"' in create_response.text

    task_id = next(iter(state.harness_runtime.tasks))
    approve_response = client.post(f"/dags/{task_id}/approve")
    assert approve_response.status_code == 200
    assert approve_response.json()["dag"]["status"] == "approved"

    execute_response = client.post(f"/dags/{task_id}/execute")
    assert execute_response.status_code == 200
    assert execute_response.json()["dag"]["status"] == "completed"


def test_api_message_stream_can_return_direct_answer_without_dag() -> None:
    provider = MockProvider([ChatResponse(content="hello there")])
    state.harness_runtime = HarnessRuntime(
        agent_loop=AgentLoop(
            provider=provider,
            tool_executor=ToolExecutor(ToolRegistry()),
        ),
        dag_creator=LLMDagCreator(provider, profile=_profile("dag_creator")),
        dag_executor=DAGExecutor(agent_loop=CompletingLoop()),
        conversation_profile=_profile("conversation"),
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "hello", "mode": "auto"},
    )

    assert response.status_code == 200
    assert "hello there" in response.text
    assert '"dag": null' in response.text


def _dag_creator_json(
    *,
    tools: list[str] | None = None,
    boundary_mode: str = "read_only",
) -> str:
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
                    "tools": tools or [],
                    "skills": [],
                    "boundary": {
                        "mode": boundary_mode,
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


def _profile(name: str) -> AgentProfile:
    return AgentProfile(
        name=name,
        role=name,
        layers=["soul"],
        layer_contents={"soul": f"You are {name}."},
    )
