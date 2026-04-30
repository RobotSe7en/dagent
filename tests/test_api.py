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
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import Boundary
from dagent.tools.boundary import BoundaryViolation
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


class PermissionThenCompletingLoop:
    def __init__(self) -> None:
        self.calls = 0

    async def run(
        self,
        user_message: str,
        *,
        boundary: Boundary,
        max_steps: int = 8,
        allowed_tools: list[str] | None = None,
        messages: list[dict] | None = None,
    ) -> AgentLoopResult:
        self.calls += 1
        if self.calls == 1:
            raise BoundaryViolation(
                "Command 'python --version' is not allowed.",
                command="python --version",
            )
        return AgentLoopResult(
            final_response="done after permission",
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


def test_api_can_approve_pending_permission_and_resume_dag() -> None:
    provider = MockProvider([ChatResponse(content=_dag_creator_json())])
    loop = PermissionThenCompletingLoop()
    state.control_plane = ControlPlane(
        dag_creator=LLMDagCreator(provider),
        executor=DAGExecutor(agent_loop=loop),
    )
    state.harness_runtime = None
    state.runs.clear()
    client = TestClient(app)

    task_response = client.post(
        "/tasks",
        json={"message": "make a small DAG", "task_id": "task_api_permission"},
    )
    assert task_response.status_code == 200

    first_execute = client.post("/dags/task_api_permission/execute")
    assert first_execute.status_code == 200
    first_payload = first_execute.json()
    assert first_payload["dag"]["status"] == "paused_for_permission"
    request = first_payload["result"]["pending_permission_request"]
    assert request["node_id"] == "answer"
    assert request["requested_boundary"]["allowed_commands"] == ["python"]

    approve_response = client.post(
        "/dags/task_api_permission/permissions/approve",
        json={"boundary": request["requested_boundary"]},
    )
    assert approve_response.status_code == 200
    assert approve_response.json()["permission_request"]["status"] == "approved"

    second_execute = client.post("/dags/task_api_permission/execute")
    assert second_execute.status_code == 200
    second_payload = second_execute.json()
    assert second_payload["dag"]["status"] == "completed"
    assert second_payload["result"]["completed"] is True
    assert loop.calls == 2


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


def test_api_message_stream_interleaves_tool_events() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="echo",
                        arguments={"text": "hi"},
                    )
                ]
            ),
            ChatResponse(content="done"),
        ]
    )
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
    state.harness_runtime = HarnessRuntime(
        agent_loop=AgentLoop(
            provider=provider,
            tool_executor=ToolExecutor(registry),
        ),
        dag_creator=LLMDagCreator(provider, profile=_profile("dag_creator")),
        dag_executor=DAGExecutor(agent_loop=CompletingLoop()),
        conversation_profile=_profile("conversation"),
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "echo hi", "mode": "direct"},
    )

    assert response.status_code == 200
    lines = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
    event_types = [json.loads(line)["type"] for line in lines]
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert event_types.index("tool_call") < event_types.index("tool_result") < event_types.index("done")
    assert '"content": "echo:hi"' in response.text


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
