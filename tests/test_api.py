import json

from fastapi.testclient import TestClient

from dagent.api.app import app, state
from dagent.harness_runtime import (
    ToolAgent,
    ToolAgentLoop,
    DAGAgent,
    DAGAgentLoop,
    DAGExecutor,
    HarnessRuntime,
    RunnableExecutor,
)
from dagent.capabilities import RunnableRegistry
from dagent.capabilities.providers import ToolRunnableProvider
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.tools.registry import ToolRegistry


def test_api_message_stream_can_return_tool_answer_without_dag() -> None:
    state.harness_runtime = _runtime(MockProvider([
        ChatResponse(content="tool"),            # _route()
        ChatResponse(content="hello there"),     # ToolAgentLoop
    ]))
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "hello", "mode": "auto"},
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    assert events[-1]["type"] == "done"
    assert events[-1]["dag"] is None
    assert events[-1]["final_answer"] == "hello there"


def test_api_message_stream_creates_dag_and_waits_for_review() -> None:
    state.harness_runtime = _runtime(
        MockProvider([
            ChatResponse(content="dag"),           # _route()
            ChatResponse(content=_dag_agent_dsl()),  # DAG agent
        ])
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "echo ok through a DAG", "mode": "auto", "review_level": "careful"},
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    event_types = [event["type"] for event in events]
    assert "dag" in event_types
    assert event_types[-1] == "done"
    assert events[-1]["status"] == "awaiting_review"
    assert events[-1]["pending_review"]["kind"] == "initial_dag"
    assert events[-1]["dag"]["status"] == "review_required"
    assert events[-1]["final_answer"] == ""


def test_api_fast_dag_streams_planning_think_and_live_trace() -> None:
    state.harness_runtime = _runtime(
        MockProvider([
            ChatResponse(content="dag"),                                          # _route()
            ChatResponse(content="<think>planning dag</think>\n" + _dag_agent_dsl()),  # DAG agent
            ChatResponse(content="Final answer: echo:ok"),                        # execute observation
        ])
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "echo ok through a DAG", "mode": "auto", "review_level": "fast"},
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    done_index = next(index for index, event in enumerate(events) if event["type"] == "done")
    token_text = "".join(event.get("content", "") for event in events if event["type"] == "token")
    assert "<think>planning dag</think>" in token_text
    assert any(event.get("type") == "trace" for event in events[:done_index])
    assert events[-1]["status"] == "completed"
    assert events[-1]["dag"]["status"] == "completed"


def test_api_fast_dag_streams_failed_and_replanned_dag_versions() -> None:
    state.harness_runtime = _runtime(
        MockProvider([
            ChatResponse(content="dag"),                                              # _route()
            ChatResponse(content='task: fail first\nbad = fail_tool(text="boom")\n'),  # DAG agent initial
            ChatResponse(content='task: repaired\nanswer = echo(text="ok")\n'),        # replan
            ChatResponse(content="Recovered after replanning."),                       # execute observation
        ])
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "repair through DAG", "mode": "auto", "review_level": "fast"},
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    dag_events = [event["dag"] for event in events if event["type"] == "dag"]
    assert any(dag["version"] == 1 and dag["status"] == "failed" for dag in dag_events)
    assert any(dag["version"] == 2 and dag["status"] == "completed" for dag in dag_events)
    assert events[-1]["status"] == "completed"


def test_api_dag_mode_returns_failed_fast_dag_answer() -> None:
    state.harness_runtime = _runtime(
        MockProvider([
            ChatResponse(content='task: fail\nbad = fail_tool(text="boom")\n'),
            ChatResponse(content="NO_CHANGE"),
            ChatResponse(content="The DAG failed after retrying the failing node."),
        ])
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "run a failing DAG", "mode": "dag", "review_level": "fast"},
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    assert events[-1]["status"] == "failed"
    assert events[-1]["dag"]["status"] == "failed"


def test_api_resume_executes_reviewed_dag_and_trace_endpoint_reads_records() -> None:
    state.harness_runtime = _runtime(
        MockProvider([
            ChatResponse(content="dag"),            # _route()
            ChatResponse(content=_dag_agent_dsl()),  # DAG agent
            ChatResponse(content="Final answer: echo:ok"),  # execute observation
        ])
    )
    client = TestClient(app)

    stream_response = client.post(
        "/messages/stream",
        json={"message": "echo ok through a DAG", "mode": "auto", "review_level": "careful"},
    )
    stream_events = _sse_events(stream_response.text)
    task_id = stream_events[-1]["task_id"]
    review_id = stream_events[-1]["pending_review"]["review_id"]
    dag = stream_events[-1]["dag"]
    dag["nodes"][0]["invocation"]["arguments"] = {"text": "reviewed"}

    resume_response = client.post(
        "/messages/resume",
        json={"review_id": review_id, "dag": dag},
    )

    assert resume_response.status_code == 200
    resume_events = _sse_events(resume_response.text)
    assert resume_events[-1]["status"] == "completed"
    assert resume_events[-1]["dag"]["status"] == "completed"
    assert any(event.get("event", {}).get("event_type") == "tool_completed" for event in resume_events)

    trace_response = client.get(f"/tasks/{task_id}/trace")
    assert trace_response.status_code == 200
    records = trace_response.json()["records"]
    assert len(records) == 1
    assert records[0]["node_id"] == "answer"
    assert records[0]["tool"] == "tool.echo"
    assert records[0]["args"] == {"text": "reviewed"}
    assert records[0]["output"] == "echo:reviewed"
    assert records[0]["status"] == "completed"


def test_api_resume_review_returns_final_answer_without_streaming_answer_text() -> None:
    state.harness_runtime = _runtime(
        MockProvider([
            ChatResponse(content="dag"),            # _route()
            ChatResponse(content=_dag_agent_dsl()),  # DAG agent
            ChatResponse(content="<think>observe</think>DAG agent final answer"),
        ])
    )
    client = TestClient(app)

    stream_response = client.post(
        "/messages/stream",
        json={"message": "echo ok through a DAG", "mode": "auto", "review_level": "careful"},
    )
    stream_events = _sse_events(stream_response.text)
    review_id = stream_events[-1]["pending_review"]["review_id"]
    dag = stream_events[-1]["dag"]

    resume_response = client.post(
        "/messages/resume",
        json={"review_id": review_id, "dag": dag},
    )

    assert resume_response.status_code == 200
    events = _sse_events(resume_response.text)
    token_text = "".join(event.get("content", "") for event in events if event["type"] == "token")
    assert "<think>observe</think>" in token_text
    assert "DAG agent final answer" not in token_text
    assert events[-1]["final_answer"] == "DAG agent final answer"


def test_api_old_dag_lifecycle_routes_are_removed() -> None:
    state.harness_runtime = _runtime(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    assert client.post("/tasks", json={"message": "old path"}).status_code == 404
    assert client.post("/dags/task_api/approve").status_code == 404
    assert client.post("/dags/task_api/execute").status_code == 404
    assert client.put("/dags/task_api", json={"dag": {}}).status_code == 404


def test_api_runnable_list_create_and_test() -> None:
    state.harness_runtime = _runtime(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    list_response = client.get("/runnables")

    assert list_response.status_code == 200
    runnable_ids = {item["id"] for item in list_response.json()["runnables"]}
    assert {"tool.echo", "memory.write", "file.write"}.issubset(runnable_ids)

    create_response = client.post(
        "/runnables",
        json={
            "id": "custom_tool.upper",
            "name": "upper",
            "kind": "custom_tool",
            "description": "Uppercase text.",
            "config": {"template": "upper:{text}"},
        },
    )
    assert create_response.status_code == 200

    test_response = client.post(
        "/runnables/custom_tool.upper/test",
        json={"arguments": {"text": "ok"}},
    )

    assert test_response.status_code == 200
    assert test_response.json()["result"]["content"] == "upper:ok"

    disable_response = client.post("/runnables/custom_tool.upper/disable")
    assert disable_response.status_code == 200
    assert disable_response.json()["runnable"]["enabled"] is False

    enable_response = client.post("/runnables/custom_tool.upper/enable")
    assert enable_response.status_code == 200
    assert enable_response.json()["runnable"]["enabled"] is True

    delete_response = client.delete("/runnables/custom_tool.upper")
    assert delete_response.status_code == 200
    assert client.post(
        "/runnables/custom_tool.upper/test",
        json={"arguments": {"text": "ok"}},
    ).status_code == 404


def test_api_capability_status_endpoints() -> None:
    state.harness_runtime = _runtime(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    assert client.get("/mcp/servers").status_code == 200
    assert client.get("/skills").status_code == 200
    sandbox = client.get("/sandbox/status")

    assert sandbox.status_code == 200
    assert sandbox.json()["runner"] in {"local-dev", "container"}


def _runtime(provider: MockProvider) -> HarnessRuntime:
    runnable_executor = _runnable_executor()
    dag_executor = DAGExecutor(runnable_executor=runnable_executor)
    return HarnessRuntime(
        provider=provider,
        tool_agent=ToolAgent(
            loop=ToolAgentLoop(provider=provider, runnable_executor=runnable_executor),
            profile=_profile("conversation"),
            tools=runnable_executor.registry.list(kind="tool", enabled_only=True),
        ),
        dag_agent=DAGAgent(
            loop=DAGAgentLoop(
                provider=provider,
                dag_executor=dag_executor,
            ),
            profile=_profile("dag_agent"),
            tools=runnable_executor.registry.list(kind="tool", enabled_only=True),
        ),
        runnable_registry=runnable_executor.registry,
        runnable_executor=runnable_executor,
    )


def _runnable_executor() -> RunnableExecutor:
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
    registry.register(
        name="fail_tool",
        handler=lambda text: (_ for _ in ()).throw(RuntimeError(f"failed:{text}")),
        action="read",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    runnable_registry = RunnableRegistry()
    runnable_executor = RunnableExecutor(runnable_registry)
    ToolRunnableProvider(registry).register_into(runnable_registry, runnable_executor)
    return runnable_executor


def _dag_agent_dsl() -> str:
    return 'task: mock\nanswer = echo(text="ok")\n'


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
