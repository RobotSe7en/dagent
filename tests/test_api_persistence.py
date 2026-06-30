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
from dagent import RunState
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
