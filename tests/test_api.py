import json

from fastapi.testclient import TestClient

from dagent.api.app import app, state
from dagent.harness_runtime import AgentLoop, DAGExecutor, HarnessRuntime, LLMDagCreator
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.tools.executor import ToolExecutor
from dagent.tools.registry import ToolRegistry


def test_api_message_stream_can_return_direct_answer_without_dag() -> None:
    state.harness_runtime = _runtime(MockProvider([ChatResponse(content="hello there")]))
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "hello", "mode": "auto"},
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    assert [event["type"] for event in events] == ["status", "token", "done"]
    assert events[-1]["dag"] is None
    assert events[-1]["message_markdown"] == "hello there"


def test_api_message_stream_creates_dag_and_waits_for_review() -> None:
    state.harness_runtime = _runtime(
        MockProvider(
            [
                ChatResponse(
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="dag_creator",
                            arguments={
                                "request": "Create a safe DAG.",
                                "reason": "Needs execution.",
                            },
                        )
                    ]
                ),
                ChatResponse(content=_dag_creator_json()),
            ]
        )
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "echo ok through a DAG", "mode": "auto"},
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    event_types = [event["type"] for event in events]
    assert "dag" in event_types
    assert event_types[-1] == "done"
    assert events[-1]["status"] == "awaiting_dag_review"
    assert events[-1]["dag"]["status"] == "review_required"
    assert "DAG" in events[-1]["message_markdown"]


def test_api_resume_executes_reviewed_dag_and_trace_endpoint_reads_records() -> None:
    state.harness_runtime = _runtime(
        MockProvider(
            [
                ChatResponse(
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="dag_creator",
                            arguments={"request": "Create a safe DAG.", "reason": "Needs execution."},
                        )
                    ]
                ),
                ChatResponse(content=_dag_creator_json()),
                ChatResponse(content="Final answer: echo:ok"),
            ]
        )
    )
    client = TestClient(app)

    stream_response = client.post(
        "/messages/stream",
        json={"message": "echo ok through a DAG", "mode": "auto"},
    )
    task_id = _sse_events(stream_response.text)[-1]["task_id"]

    dag = _sse_events(stream_response.text)[-1]["dag"]
    dag["nodes"][0]["args"] = {"text": "reviewed"}

    resume_response = client.post(
        "/messages/resume",
        json={"task_id": task_id, "dag": dag},
    )

    assert resume_response.status_code == 200
    resume_events = _sse_events(resume_response.text)
    assert resume_events[-1]["status"] == "completed"
    assert resume_events[-1]["dag"]["status"] == "completed"
    assert resume_events[-1]["message_markdown"] == "Final answer: echo:ok"
    assert any(event.get("event", {}).get("event_type") == "tool_completed" for event in resume_events)

    trace_response = client.get(f"/tasks/{task_id}/trace")
    assert trace_response.status_code == 200
    records = trace_response.json()["records"]
    assert len(records) == 1
    assert records[0]["node_id"] == "answer"
    assert records[0]["tool"] == "echo"
    assert records[0]["args"] == {"text": "reviewed"}
    assert records[0]["output"] == "echo:reviewed"
    assert records[0]["status"] == "completed"


def test_api_old_dag_lifecycle_routes_are_removed() -> None:
    state.harness_runtime = _runtime(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    assert client.post("/tasks", json={"message": "old path"}).status_code == 404
    assert client.post("/dags/task_api/approve").status_code == 404
    assert client.post("/dags/task_api/execute").status_code == 404
    assert client.put("/dags/task_api", json={"dag": {}}).status_code == 404


def _runtime(provider: MockProvider) -> HarnessRuntime:
    tool_executor = _tool_executor()
    return HarnessRuntime(
        agent_loop=AgentLoop(provider=provider, tool_executor=tool_executor),
        dag_creator=LLMDagCreator(provider, profile=_profile("dag_creator")),
        dag_executor=DAGExecutor(
            agent_loop=AgentLoop(provider=MockProvider([]), tool_executor=tool_executor),
            tool_executor=tool_executor,
        ),
        conversation_profile=_profile("conversation"),
        auto_execute_approved_dags=True,
    )


def _tool_executor() -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(
        name="dag_start",
        handler=lambda: "started",
        action="read",
        parameters={
            "type": "object",
            "properties": {},
        },
    )
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
    return ToolExecutor(registry)


def _dag_creator_json() -> str:
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
                    "kind": "tool",
                    "tool": "echo",
                    "args": {"text": "ok"},
                    "agent": None,
                    "tools": ["echo"],
                    "skills": [],
                    "boundary": {
                        "mode": "read_only",
                        "allowed_paths": [],
                        "forbidden_tools": [],
                        "allowed_commands": [],
                        "forbidden_commands": [],
                    },
                    "risk": "low",
                    "risk_reason": "No risky access.",
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


def _sse_events(text: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in text.splitlines()
        if line.startswith("data: ")
    ]
