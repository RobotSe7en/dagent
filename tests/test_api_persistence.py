import json
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from fastapi.testclient import TestClient

from api.app import app, state
from api.storage import (
    ConversationBusyError,
    SQLiteStore,
)
from api.workspaces import LocalWorkspaceStore
from dagent import RunState, Runner
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import RunTrace, RunTraceNode


@pytest.fixture
def persistence_client(monkeypatch, tmp_path: Path):
    state.close_runner()
    store = SQLiteStore(tmp_path / "api.sqlite3")
    workspaces = LocalWorkspaceStore(tmp_path / "projects")
    monkeypatch.setattr(state, "store", store, raising=False)
    monkeypatch.setattr(state, "workspaces", workspaces, raising=False)
    try:
        yield TestClient(app)
    finally:
        state.close_runner()
        store.close()
        monkeypatch.setattr(state, "store", None, raising=False)
        monkeypatch.setattr(state, "workspaces", None, raising=False)


def test_local_workspace_store_uses_project_workspace_without_run_directory(tmp_path: Path) -> None:
    store = LocalWorkspaceStore(tmp_path / ".dagent" / "projects")

    uri = store.project_workspace_uri("proj_123")
    path = store.local_path_for(uri)

    assert uri == f"file://{tmp_path / '.dagent' / 'projects' / 'proj_123' / 'workspace'}"
    assert path == (tmp_path / ".dagent" / "projects" / "proj_123" / "workspace").resolve()
    assert path.is_dir()
    assert path.name == "workspace"


def test_sqlite_store_persists_projects_conversations_and_run_state(tmp_path: Path) -> None:
    db_path = tmp_path / "api.sqlite3"
    workspace_uri = f"file://{tmp_path / 'projects' / 'proj_123' / 'workspace'}"
    store = SQLiteStore(db_path)
    project = store.create_project(
        project_id="proj_123",
        slug="demo",
        name="Demo",
        workspace_uri=workspace_uri,
    )
    conversation = store.create_conversation(
        conversation_id="conv_123",
        project_id=project.id,
        title="Initial chat",
    )
    run_state = RunState(
        run_id="run_123",
        kind="tool",
        status="completed",
        workspace_path=str(tmp_path / "projects" / "proj_123" / "workspace"),
        trace=RunTrace(
            run_id="run_123",
            root=RunTraceNode.run(run_id="run_123", status="completed"),
            status="completed",
        ),
    )

    run = store.create_run(
        run_id=run_state.run_id,
        project_id=project.id,
        conversation_id=conversation.id,
        user_id="user_123",
        kind="tool",
        status="running",
        workspace_uri=workspace_uri,
    )
    event = store.append_run_event(
        run_id=run.id,
        stream_id="stream_123",
        event_type="run.started",
        payload_json='{"type":"run.started"}',
    )
    store.save_run_state(run.id, run_state.model_dump_json(), output_text="done")
    store.update_run_status(run.id, "completed", completed_at=int(time.time()))
    store.close()

    reopened = SQLiteStore(db_path)
    recovered_project = reopened.get_project("proj_123")
    recovered_run = reopened.get_run("run_123")
    recovered_state = reopened.get_run_state("run_123")
    events = reopened.list_run_events("run_123")

    assert recovered_project is not None
    assert recovered_project.workspace_uri == workspace_uri
    assert recovered_run is not None
    assert recovered_run.status == "completed"
    assert recovered_run.output_text == "done"
    assert recovered_state is not None
    assert recovered_state.run_id == "run_123"
    assert recovered_state.trace is not None
    assert recovered_state.trace.status == "completed"
    assert events == [event]


def test_run_events_have_durable_service_sequence(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "api.sqlite3")
    store.create_project(
        project_id="proj_123",
        slug="demo",
        name="Demo",
        workspace_uri=f"file://{tmp_path / 'workspace'}",
    )
    store.create_run(
        run_id="run_123",
        project_id="proj_123",
        conversation_id=None,
        user_id="user_123",
        kind="tool",
        status="running",
        workspace_uri=f"file://{tmp_path / 'workspace'}",
    )

    first = store.append_run_event(
        run_id="run_123",
        stream_id="stream_a",
        event_type="run.started",
        payload_json='{"sequence":0}',
    )
    second = store.append_run_event(
        run_id="run_123",
        stream_id="stream_b",
        event_type="run.finished",
        payload_json='{"sequence":0}',
    )

    assert first.event_id == 1
    assert first.stream_seq == 0
    assert second.event_id == 2
    assert second.stream_seq == 0
    assert store.list_run_events("run_123", after_event_id=1) == [second]


def test_conversation_lock_is_single_writer_and_releasable(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "api.sqlite3")
    store.create_project(
        project_id="proj_123",
        slug="demo",
        name="Demo",
        workspace_uri=f"file://{tmp_path / 'workspace'}",
    )
    store.create_conversation(
        conversation_id="conv_123",
        project_id="proj_123",
        title="Initial chat",
    )

    lock = store.acquire_conversation_lock("conv_123", owner="stream_a")

    with pytest.raises(ConversationBusyError):
        store.acquire_conversation_lock("conv_123", owner="stream_b")

    lock.release()
    second_lock = store.acquire_conversation_lock("conv_123", owner="stream_b")
    second_lock.release()


def test_conversation_lock_is_thread_safe(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "api.sqlite3")
    store.create_project(
        project_id="proj_123",
        slug="demo",
        name="Demo",
        workspace_uri=f"file://{tmp_path / 'workspace'}",
    )
    store.create_conversation(
        conversation_id="conv_123",
        project_id="proj_123",
        title="Initial chat",
    )
    acquired: list[str] = []
    failures: list[str] = []

    def try_lock(owner: str) -> None:
        try:
            lock = store.acquire_conversation_lock("conv_123", owner=owner)
        except ConversationBusyError:
            failures.append(owner)
            return
        acquired.append(owner)
        time.sleep(0.05)
        lock.release()

    threads = [
        threading.Thread(target=try_lock, args=(owner,))
        for owner in ("stream_a", "stream_b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(acquired) == 1
    assert len(failures) == 1


def test_api_creates_project_with_shared_project_workspace(persistence_client) -> None:
    response = persistence_client.post(
        "/projects",
        json={"name": "Demo Project", "slug": "demo-project", "description": "Prototype"},
    )

    assert response.status_code == 200
    project = response.json()["project"]
    workspace_uri = project["workspace_uri"]
    workspace_path = Path(unquote(urlparse(workspace_uri).path))
    assert project["id"].startswith("proj_")
    assert project["slug"] == "demo-project"
    assert workspace_path.is_dir()
    assert workspace_path.name == "workspace"
    assert workspace_path.parent.name == project["id"]
    assert not (workspace_path / project["id"]).exists()

    listed = persistence_client.get("/projects")
    fetched = persistence_client.get(f"/projects/{project['id']}")

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["projects"]] == [project["id"]]
    assert fetched.status_code == 200
    assert fetched.json()["project"]["workspace_uri"] == workspace_uri


def test_api_project_slug_must_be_unique(persistence_client) -> None:
    first = persistence_client.post("/projects", json={"name": "Demo", "slug": "demo"})
    duplicate = persistence_client.post("/projects", json={"name": "Demo 2", "slug": "demo"})

    assert first.status_code == 200
    assert duplicate.status_code == 400
    assert "slug" in duplicate.json()["detail"]


def test_api_creates_and_lists_project_conversations(persistence_client) -> None:
    project_response = persistence_client.post("/projects", json={"name": "Demo", "slug": "demo"})
    project_id = project_response.json()["project"]["id"]

    response = persistence_client.post(
        f"/projects/{project_id}/conversations",
        json={"title": "First chat"},
    )
    listed = persistence_client.get(f"/projects/{project_id}/conversations")

    assert response.status_code == 200
    conversation = response.json()["conversation"]
    assert conversation["id"].startswith("conv_")
    assert conversation["project_id"] == project_id
    assert conversation["title"] == "First chat"
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["conversations"]] == [conversation["id"]]


def test_api_rejects_conversation_for_missing_project(persistence_client) -> None:
    response = persistence_client.post(
        "/projects/proj_missing/conversations",
        json={"title": "No project"},
    )

    assert response.status_code == 404


def test_api_project_message_stream_persists_run_events_and_state(
    persistence_client,
) -> None:
    state.runner = Runner(
        provider=MockProvider([
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="tool_write_file",
                        arguments={"path": "notes/shared.txt", "content": "hello"},
                    )
                ]
            ),
            ChatResponse(content="done"),
        ])
    )
    project = persistence_client.post(
        "/projects",
        json={"name": "Demo", "slug": "demo"},
    ).json()["project"]
    conversation = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "First chat"},
    ).json()["conversation"]
    workspace_path = Path(unquote(urlparse(project["workspace_uri"]).path))

    response = persistence_client.post(
        "/messages/stream",
        json={
            "messages": [{"role": "user", "content": "write the note"}],
            "target": "tool",
            "capability_ids": ["tool.write_file"],
            "project_id": project["id"],
            "conversation_id": conversation["id"],
        },
    )
    events = _sse_events(response.text)
    result = events[-1]["data"]["result"]
    run_id = result["state"]["run_id"]
    store = state.get_store()

    assert response.status_code == 200
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert (workspace_path / "notes" / "shared.txt").read_text(encoding="utf-8") == "hello"
    assert not (workspace_path / run_id).exists()
    persisted_run = store.get_run(run_id)
    persisted_state = store.get_run_state(run_id)
    persisted_events = store.list_run_events(run_id)
    listed_runs = persistence_client.get(
        f"/projects/{project['id']}/conversations/{conversation['id']}/runs"
    )
    run_response = persistence_client.get(f"/runs/{run_id}")
    replay_response = persistence_client.get(f"/runs/{run_id}/events", params={"after_event_id": 1})

    assert persisted_run is not None
    assert persisted_run.project_id == project["id"]
    assert persisted_run.conversation_id == conversation["id"]
    assert persisted_run.status == "completed"
    assert persisted_run.output_text == "done"
    assert persisted_state is not None
    assert persisted_state.workspace_path == str(workspace_path)
    assert len(persisted_events) == len(events)
    assert [event.event_id for event in persisted_events] == list(range(1, len(events) + 1))
    assert listed_runs.status_code == 200
    assert [item["id"] for item in listed_runs.json()["runs"]] == [run_id]
    assert run_response.status_code == 200
    assert run_response.json()["run"]["id"] == run_id
    assert "state_json" not in run_response.json()["run"]
    assert run_response.json()["run"]["has_state"] is True
    assert replay_response.status_code == 200
    assert [event["event_id"] for event in replay_response.json()["events"]] == list(range(2, len(events) + 1))
    assert replay_response.json()["events"][0]["payload"]["sequence"] == 2

    state.close_runner()
    trace_response = persistence_client.get(f"/runs/{run_id}/trace")
    artifacts_response = persistence_client.get(f"/runs/{run_id}/artifacts")
    preview_response = persistence_client.get(
        f"/runs/{run_id}/artifacts/preview",
        params={"path": "notes/shared.txt"},
    )

    assert trace_response.status_code == 200
    assert trace_response.json()["trace"]["run_id"] == run_id
    assert trace_response.json()["trace"]["root"]["status"] == "completed"
    assert artifacts_response.status_code == 200
    files = {item["path"]: item for item in artifacts_response.json()["files"]}
    assert "notes/shared.txt" in files
    assert preview_response.status_code == 200
    assert preview_response.json()["content"] == "hello"


def test_api_project_message_stream_rejects_client_workspace_root(persistence_client) -> None:
    project = persistence_client.post(
        "/projects",
        json={"name": "Demo", "slug": "demo"},
    ).json()["project"]
    conversation = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "First chat"},
    ).json()["conversation"]

    response = persistence_client.post(
        "/messages/stream",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "project_id": project["id"],
            "conversation_id": conversation["id"],
            "workspace_root": "custom",
        },
    )

    assert response.status_code == 400
    assert "workspace_root" in response.json()["detail"]


def test_api_project_message_stream_locks_only_the_conversation(persistence_client) -> None:
    project = persistence_client.post(
        "/projects",
        json={"name": "Demo", "slug": "demo"},
    ).json()["project"]
    first = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "First chat"},
    ).json()["conversation"]
    second = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "Second chat"},
    ).json()["conversation"]
    lock = state.get_store().acquire_conversation_lock(first["id"], owner="manual")
    state.runner = Runner(provider=MockProvider([ChatResponse(content="done")]))
    try:
        busy = persistence_client.post(
            "/messages/stream",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "target": "tool",
                "project_id": project["id"],
                "conversation_id": first["id"],
            },
        )
        other_conversation = persistence_client.post(
            "/messages/stream",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "target": "tool",
                "project_id": project["id"],
                "conversation_id": second["id"],
            },
        )
    finally:
        lock.release()

    assert busy.status_code == 409
    assert other_conversation.status_code == 200
    assert _sse_events(other_conversation.text)[-1]["type"] == "run.finished"


def test_api_project_review_resume_uses_db_state_after_runner_restart(
    persistence_client,
    tmp_path: Path,
) -> None:
    provider = MockProvider([
        ChatResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="tool_read_file",
                    arguments={"path": "../blocked/secret.txt"},
                )
            ],
        )
    ])
    state.runner = Runner(provider=provider)
    project = persistence_client.post(
        "/projects",
        json={"name": "Demo", "slug": "demo"},
    ).json()["project"]
    conversation = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "First chat"},
    ).json()["conversation"]
    workspace_path = Path(unquote(urlparse(project["workspace_uri"]).path))
    blocked_dir = workspace_path.parent / "blocked"
    blocked_dir.mkdir(parents=True)
    (blocked_dir / "secret.txt").write_text("private", encoding="utf-8")

    stream_response = persistence_client.post(
        "/messages/stream",
        json={
            "messages": [{"role": "user", "content": "read outside"}],
            "target": "tool",
            "capability_ids": ["tool.read_file"],
            "project_id": project["id"],
            "conversation_id": conversation["id"],
        },
    )
    stream_events = _sse_events(stream_response.text)
    stream_result = stream_events[-1]["data"]["result"]
    run_id = stream_result["state"]["run_id"]
    review_id = stream_result["state"]["pending_review"]["review_id"]
    assert stream_result["state"]["status"] == "awaiting_review"

    state.close_runner()
    state.runner = Runner(provider=MockProvider([ChatResponse(content="I will stop.")]))
    resume_response = persistence_client.post(
        f"/projects/{project['id']}/reviews/{review_id}/resume",
        json={"approved": False, "feedback": "Do not read that file."},
    )
    resume_events = _sse_events(resume_response.text)
    resume_result = resume_events[-1]["data"]["result"]
    review = state.get_store().get_review(review_id)
    run_state = state.get_store().get_run_state(run_id)

    assert resume_response.status_code == 200
    assert resume_result["state"]["run_id"] == run_id
    assert resume_result["state"]["status"] == "completed"
    assert resume_result["output_text"] == "I will stop."
    assert review is not None
    assert review.status == "resolved"
    assert run_state is not None
    assert run_state.status == "completed"


def _sse_events(text: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for block in text.strip().split("\n\n"):
        if not block:
            continue
        line = block.strip()
        assert line.startswith("data: ")
        events.append(json.loads(line.removeprefix("data: ")))
    return events
