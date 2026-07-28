import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlparse, urlsplit

import pytest
from fastapi.testclient import TestClient

import api.app as api_app
from api.app import app, state
from api.storage import (
    ConversationBusyError,
    SQLiteStore,
)
from api.workspaces import LocalWorkspaceStore
from dagent import (
    CapabilityInvocation,
    ConversationState,
    DAG,
    PendingReview,
    RunResult,
    RunState,
    RunStreamEvent,
    Runner,
)
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.result import RunFinishedData, RunStartedData
from dagent.schemas import PendingCapabilityCall, RunTrace, RunTraceNode
from tests.planner_helpers import capability_plan_response, final_answer_response


def _echo_dag(*, status: str = "review_required") -> DAG:
    return DAG.model_validate({
        "dag_id": "dag_echo",
        "task_id": "run_echo",
        "version": 1,
        "status": status,
        "nodes": [
            {
                "id": "answer",
                "payload": {
                    "type": "capability",
                    "invocation": {
                        "capability_id": "tool.write_file",
                        "kind": "tool",
                        "arguments": {"path": "review.txt", "content": "ok"},
                        "boundary": {},
                    },
                },
            }
        ],
        "edges": [],
    })


@pytest.fixture
def persistence_client(monkeypatch, tmp_path: Path):
    state.close_runner()
    store = SQLiteStore(tmp_path / "api.sqlite3")
    workspaces = LocalWorkspaceStore(tmp_path / "projects")
    monkeypatch.setattr(state, "store", store, raising=False)
    monkeypatch.setattr(state, "workspaces", workspaces, raising=False)
    monkeypatch.setattr(state, "get_user_config_path", lambda: tmp_path / "user-config.yaml")
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


def test_local_workspace_store_uses_conversation_workspace(tmp_path: Path) -> None:
    store = LocalWorkspaceStore(tmp_path / ".dagent" / "projects")

    standalone_uri = store.conversation_workspace_uri("conv_standalone")
    project_uri = store.conversation_workspace_uri("conv_project", project_id="proj_123")
    standalone_path = store.local_path_for(standalone_uri)
    project_path = store.local_path_for(project_uri)

    assert standalone_uri == f"file://{tmp_path / '.dagent' / 'projects' / '_conversations' / 'conv_standalone' / 'workspace'}"
    assert project_uri == f"file://{tmp_path / '.dagent' / 'projects' / 'proj_123' / 'workspace'}"
    assert standalone_path.is_dir()
    assert project_path.is_dir()
    assert standalone_path.name == "workspace"
    assert project_path.name == "workspace"
    assert project_path.parent.name == "proj_123"


def test_local_workspace_store_builds_hostless_windows_file_uri() -> None:
    store = LocalWorkspaceStore.__new__(LocalWorkspaceStore)
    store.root = PureWindowsPath(r"C:\Users\Administrator\.dagent\projects")

    uri = store.conversation_workspace_uri("conv_123")
    parsed = urlparse(uri)

    assert uri == "file:///C:/Users/Administrator/.dagent/projects/_conversations/conv_123/workspace"
    assert parsed.scheme == "file"
    assert parsed.netloc == ""


def test_local_workspace_store_rejects_paths_outside_root(tmp_path: Path) -> None:
    store = LocalWorkspaceStore(tmp_path / ".dagent" / "projects")

    with pytest.raises(ValueError, match="workspace root"):
        store.local_path_for(f"file://{tmp_path / 'outside' / 'workspace'}")


def test_local_workspace_store_open_file_rejects_relative_escape(tmp_path: Path) -> None:
    store = LocalWorkspaceStore(tmp_path / ".dagent" / "projects")
    uri = store.project_workspace_uri("proj_123")

    with pytest.raises(ValueError, match="relative"):
        store.open_file(uri, "../secret.txt")


def test_sqlite_store_persists_projects_conversations_and_run_state(tmp_path: Path) -> None:
    db_path = tmp_path / "api.sqlite3"
    workspace_uri = f"file://{tmp_path / 'projects' / 'proj_123' / 'workspace'}"
    store = SQLiteStore(db_path)
    project = store.create_project(
        project_id="proj_123",
        slug="demo",
        name="Demo",
        workspace_uri=f"file://{tmp_path / 'projects' / 'proj_123' / 'workspace'}",
    )
    conversation = store.create_conversation(
        conversation_id="conv_123",
        project_id=project.id,
        title="Initial chat",
        workspace_uri=workspace_uri,
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
    assert recovered_project.workspace_uri == f"file://{tmp_path / 'projects' / 'proj_123' / 'workspace'}"
    assert conversation.workspace_uri == workspace_uri
    assert recovered_run is not None
    assert recovered_run.status == "completed"
    assert recovered_run.output_text == "done"
    assert recovered_state is not None
    assert recovered_state.run_id == "run_123"
    assert recovered_state.trace is not None
    assert recovered_state.trace.status == "completed"
    assert events == [event]


def test_sqlite_store_persists_standalone_conversation(tmp_path: Path) -> None:
    db_path = tmp_path / "api.sqlite3"
    workspace_uri = f"file://{tmp_path / 'conversations' / 'conv_standalone' / 'workspace'}"
    store = SQLiteStore(db_path)

    conversation = store.create_conversation(
        conversation_id="conv_standalone",
        project_id=None,
        title="Inbox chat",
        workspace_uri=workspace_uri,
    )
    store.create_run(
        run_id="run_standalone",
        project_id=None,
        conversation_id=conversation.id,
        user_id="user_123",
        kind="tool",
        status="running",
        workspace_uri=workspace_uri,
    )
    store.close()

    reopened = SQLiteStore(db_path)
    recovered_conversation = reopened.get_conversation("conv_standalone")
    recovered_run = reopened.get_run("run_standalone")

    assert recovered_conversation is not None
    assert recovered_conversation.project_id is None
    assert recovered_conversation.workspace_uri == workspace_uri
    assert recovered_run is not None
    assert recovered_run.project_id is None
    assert recovered_run.workspace_uri == workspace_uri


def test_sqlite_store_persists_conversation_owner(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "api.sqlite3")

    conversation = store.create_conversation(
        conversation_id="conv_owner",
        project_id=None,
        title="Owned chat",
        workspace_uri=f"file://{tmp_path / 'workspace'}",
        owner_user_id="user_123",
    )
    store.close()

    reopened = SQLiteStore(tmp_path / "api.sqlite3")
    recovered = reopened.get_conversation("conv_owner")

    assert conversation.owner_user_id == "user_123"
    assert recovered is not None
    assert recovered.owner_user_id == "user_123"
    reopened.close()


def test_sqlite_store_recreates_incompatible_api_database(tmp_path: Path) -> None:
    db_path = tmp_path / "api.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
        CREATE TABLE projects(
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL DEFAULT 'default',
            owner_user_id TEXT NOT NULL DEFAULT 'default',
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            workspace_uri TEXT NOT NULL,
            settings_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            archived_at INTEGER
        );
        CREATE TABLE runs(
            id TEXT PRIMARY KEY,
            project_id TEXT,
            conversation_id TEXT,
            org_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL DEFAULT 'default',
            kind TEXT,
            status TEXT NOT NULL,
            execution TEXT NOT NULL DEFAULT 'local',
            workspace_uri TEXT NOT NULL,
            state_json TEXT,
            output_text TEXT NOT NULL DEFAULT '',
            error_json TEXT,
            lease_owner TEXT,
            lease_expires_at INTEGER,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            updated_at INTEGER NOT NULL
        );
        INSERT INTO schema_migrations(version, applied_at) VALUES (1, 1);
        INSERT INTO projects(
            id, slug, name, workspace_uri, created_at, updated_at
        ) VALUES ('proj_old', 'old', 'Old', 'file:///tmp/old', 1, 1);
        """
    )
    conn.close()

    store = SQLiteStore(db_path)
    columns = {
        row["name"]
        for row in store._conn.execute("PRAGMA table_info(runs)").fetchall()
    }

    assert "saved_dag_id" in columns
    assert store.get_project("proj_old") is None
    store.close()


def test_sqlite_store_persists_conversation_kind(tmp_path: Path) -> None:
    db_path = tmp_path / "api.sqlite3"
    store = SQLiteStore(db_path)

    conversation = store.create_conversation(
        conversation_id="conv_dynamic",
        project_id=None,
        title="Dynamic DAG",
        workspace_uri=f"file://{tmp_path / 'workspace'}",
        kind="dynamic_dag",
    )
    store.close()

    reopened = SQLiteStore(db_path)
    recovered = reopened.get_conversation("conv_dynamic")

    assert conversation.kind == "dynamic_dag"
    assert recovered is not None
    assert recovered.kind == "dynamic_dag"
    reopened.close()


def test_sqlite_store_updates_conversation_title(tmp_path: Path, monkeypatch) -> None:
    import api.storage.sqlite as sqlite_storage

    store = SQLiteStore(tmp_path / "api.sqlite3")
    monkeypatch.setattr(sqlite_storage.time, "time", lambda: 100)
    conversation = store.create_conversation(
        conversation_id="conv_rename",
        project_id=None,
        title="Original",
        workspace_uri=f"file://{tmp_path / 'workspace'}",
        kind="dynamic_dag",
    )
    monkeypatch.setattr(sqlite_storage.time, "time", lambda: 150)

    updated = store.update_conversation(conversation.id, title="Renamed")
    recovered = store.get_conversation(conversation.id)

    assert updated.title == "Renamed"
    assert updated.updated_at == 150
    assert recovered is not None
    assert recovered.title == "Renamed"
    assert recovered.updated_at == 150


def test_sqlite_store_filters_conversations_by_kind(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "api.sqlite3")
    workspace_uri = f"file://{tmp_path / 'workspace'}"
    store.create_conversation(
        conversation_id="conv_chat",
        project_id=None,
        title="Chat",
        workspace_uri=workspace_uri,
        kind="chat",
    )
    store.create_conversation(
        conversation_id="conv_dynamic",
        project_id=None,
        title="Dynamic",
        workspace_uri=workspace_uri,
        kind="dynamic_dag",
    )

    dynamic = store.list_conversations(standalone=True, kind="dynamic_dag")

    assert [conversation.id for conversation in dynamic] == ["conv_dynamic"]


def test_sqlite_store_persists_saved_dags_and_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "api.sqlite3"
    store = SQLiteStore(db_path)
    project = store.create_project(
        project_id="proj_dag",
        slug="dag",
        name="DAG Project",
        workspace_uri=f"file://{tmp_path / 'projects' / 'proj_dag' / 'workspace'}",
    )
    conversation = store.create_conversation(
        conversation_id="conv_static",
        project_id=project.id,
        title="Static DAG session",
        workspace_uri=project.workspace_uri,
        kind="static_dag",
    )
    spec_json = json.dumps({
        "id": "dag_saved",
        "name": "Saved DAG",
        "nodes": [],
        "edges": [],
    })

    saved = store.create_saved_dag(
        dag_id="dag_saved",
        project_id=project.id,
        name="Saved DAG",
        description="demo",
        spec_json=spec_json,
        layout_json='{"nodes":[]}',
    )
    session = store.create_orchestration_session(
        session_id="orch_static",
        conversation_id=conversation.id,
        project_id=project.id,
        kind="static_dag",
        saved_dag_id=saved.id,
        draft_dag_json=None,
        ui_state_json='{"selectedRunId":"run_1"}',
    )
    store.close()

    reopened = SQLiteStore(db_path)
    recovered_dag = reopened.get_saved_dag("dag_saved")
    project_dags = reopened.list_saved_dags(project_id=project.id)
    recovered_session = reopened.get_orchestration_session("orch_static")
    by_conversation = reopened.get_orchestration_session_by_conversation(conversation.id)

    assert recovered_dag is not None
    assert recovered_dag.project_id == project.id
    assert recovered_dag.spec_json == spec_json
    assert recovered_dag.layout_json == '{"nodes":[]}'
    assert recovered_dag.revision == 1
    assert [item.id for item in project_dags] == ["dag_saved"]
    assert recovered_session == session
    assert by_conversation == session
    reopened.close()


def test_sqlite_store_archive_saved_dag_clears_session_references(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "api.sqlite3")
    conversation = store.create_conversation(
        conversation_id="conv_static",
        project_id=None,
        title="Static DAG session",
        workspace_uri=f"file://{tmp_path / 'workspace'}",
        kind="static_dag",
    )
    saved = store.create_saved_dag(
        dag_id="dag_saved",
        project_id=None,
        name="Saved DAG",
        description="",
        spec_json='{"id":"dag_saved","name":"Saved DAG","nodes":[],"edges":[]}',
    )
    session = store.create_orchestration_session(
        session_id="orch_static",
        conversation_id=conversation.id,
        project_id=None,
        kind="static_dag",
        saved_dag_id=saved.id,
    )

    archived = store.archive_saved_dag(saved.id)
    recovered = store.get_orchestration_session(session.id)

    assert archived is True
    assert recovered is not None
    assert recovered.saved_dag_id is None


def test_sqlite_store_tracks_saved_dag_on_runs(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "api.sqlite3")
    workspace_uri = f"file://{tmp_path / 'workspace'}"
    store.create_saved_dag(
        dag_id="dag_source",
        project_id=None,
        name="Source DAG",
        description="",
        spec_json='{"id":"dag_source","name":"Source DAG","nodes":[],"edges":[]}',
        layout_json="{}",
    )

    run = store.create_run(
        run_id="run_static",
        project_id=None,
        conversation_id=None,
        user_id="user_123",
        kind="static_dag",
        status="running",
        workspace_uri=workspace_uri,
        saved_dag_id="dag_source",
    )
    recovered = store.get_run("run_static")

    assert run.saved_dag_id == "dag_source"
    assert recovered is not None
    assert recovered.saved_dag_id == "dag_source"


def test_sqlite_store_filters_runs_by_saved_dag(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "api.sqlite3")
    workspace_uri = f"file://{tmp_path / 'workspace'}"
    store.create_saved_dag(
        dag_id="dag_one",
        project_id=None,
        name="One",
        description="",
        spec_json='{"id":"one","name":"One","nodes":[],"edges":[]}',
    )
    store.create_saved_dag(
        dag_id="dag_two",
        project_id=None,
        name="Two",
        description="",
        spec_json='{"id":"two","name":"Two","nodes":[],"edges":[]}',
    )
    store.create_run(
        run_id="run_one",
        project_id=None,
        conversation_id=None,
        user_id="default",
        kind="static_dag",
        status="completed",
        workspace_uri=workspace_uri,
        saved_dag_id="dag_one",
    )
    store.create_run(
        run_id="run_two",
        project_id=None,
        conversation_id=None,
        user_id="default",
        kind="static_dag",
        status="completed",
        workspace_uri=workspace_uri,
        saved_dag_id="dag_two",
    )

    runs = store.list_runs(saved_dag_id="dag_one")

    assert [run.id for run in runs] == ["run_one"]


def test_sqlite_conversation_lock_spans_store_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "api.sqlite3"
    first = SQLiteStore(db_path)
    second = SQLiteStore(db_path)
    first.create_conversation(
        conversation_id="conv_123",
        project_id=None,
        title="Inbox",
        workspace_uri=f"file://{tmp_path / 'workspace'}",
    )

    lock = first.acquire_conversation_lock("conv_123", owner="first")
    try:
        with pytest.raises(ConversationBusyError):
            second.acquire_conversation_lock("conv_123", owner="second")
    finally:
        lock.release()

    second_lock = second.acquire_conversation_lock("conv_123", owner="second")
    second_lock.release()
    first.close()
    second.close()


def test_sqlite_conversation_lock_can_recover_after_expired_owner_crash(tmp_path: Path) -> None:
    db_path = tmp_path / "api.sqlite3"
    first = SQLiteStore(db_path)
    first.create_conversation(
        conversation_id="conv_123",
        project_id=None,
        title="Inbox",
        workspace_uri=f"file://{tmp_path / 'workspace'}",
    )
    first.acquire_conversation_lock("conv_123", owner="crashed", lease_seconds=0)
    first.close()

    reopened = SQLiteStore(db_path)
    lock = reopened.acquire_conversation_lock("conv_123", owner="recovered")
    lock.release()
    reopened.close()


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


def test_sqlite_store_deleting_run_deletes_conversation_messages(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "api.sqlite3")
    workspace_uri = f"file://{tmp_path / 'workspace'}"
    conversation = store.create_conversation(
        conversation_id="conv_run_messages",
        project_id=None,
        title="Inbox",
        workspace_uri=workspace_uri,
    )
    run = store.create_run(
        run_id="run_messages",
        project_id=None,
        conversation_id=conversation.id,
        user_id="default",
        kind="tool",
        status="completed",
        workspace_uri=workspace_uri,
    )
    store.append_conversation_message(
        message_id="msg_assistant",
        conversation_id=conversation.id,
        project_id=None,
        role="assistant",
        run_id=run.id,
        status="completed",
        content="done",
        timeline_json='[{"type":"text","content":"done"}]',
    )

    assert [message.id for message in store.list_conversation_messages(conversation.id)] == ["msg_assistant"]

    assert store.delete_run(run.id) is True
    assert store.list_conversation_messages(conversation.id) == []


def test_run_stream_activity_touches_conversation_updated_at(tmp_path: Path, monkeypatch) -> None:
    import api.storage.sqlite as sqlite_storage

    store = SQLiteStore(tmp_path / "api.sqlite3")
    monkeypatch.setattr(sqlite_storage.time, "time", lambda: 100)
    conversation = store.create_conversation(
        conversation_id="conv_touch",
        project_id=None,
        title="Inbox",
        workspace_uri=f"file://{tmp_path / 'workspace'}",
    )
    monkeypatch.setattr(sqlite_storage.time, "time", lambda: 110)
    store.create_run(
        run_id="run_touch",
        project_id=None,
        conversation_id=conversation.id,
        user_id="user_123",
        kind="tool",
        status="running",
        workspace_uri=conversation.workspace_uri,
    )
    monkeypatch.setattr(sqlite_storage.time, "time", lambda: 120)

    store.create_run_stream(
        stream_id="stream_touch",
        run_id="run_touch",
        project_id=None,
        conversation_id=conversation.id,
        user_id="user_123",
        kind="tool",
        status="running",
    )

    touched = store.get_conversation(conversation.id)
    assert touched is not None
    assert touched.updated_at == 120


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
        workspace_uri=f"file://{tmp_path / 'workspace'}",
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
        workspace_uri=f"file://{tmp_path / 'workspace'}",
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


def test_api_updates_and_deletes_project(persistence_client) -> None:
    project = persistence_client.post(
        "/projects",
        json={"name": "Demo", "slug": "demo", "description": "first"},
    ).json()["project"]
    workspace_path = Path(unquote(urlparse(project["workspace_uri"]).path))
    (workspace_path / "keep.txt").write_text("delete with project", encoding="utf-8")

    updated = persistence_client.patch(
        f"/projects/{project['id']}",
        json={"name": "Renamed", "slug": "renamed", "description": "second"},
    )
    fetched = persistence_client.get(f"/projects/{project['id']}")

    assert updated.status_code == 200
    assert updated.json()["project"]["name"] == "Renamed"
    assert updated.json()["project"]["slug"] == "renamed"
    assert updated.json()["project"]["description"] == "second"
    assert fetched.status_code == 200
    assert fetched.json()["project"]["slug"] == "renamed"

    duplicate = persistence_client.post("/projects", json={"name": "Other", "slug": "other"})
    duplicate_update = persistence_client.patch(
        f"/projects/{duplicate.json()['project']['id']}",
        json={"slug": "renamed"},
    )
    delete_response = persistence_client.delete(f"/projects/{project['id']}")

    assert duplicate_update.status_code == 400
    assert "slug" in duplicate_update.json()["detail"]
    assert delete_response.status_code == 200
    assert delete_response.json()["status"] == "deleted"
    assert persistence_client.get(f"/projects/{project['id']}").status_code == 404
    assert not workspace_path.exists()


def test_api_project_file_management(persistence_client) -> None:
    project = persistence_client.post(
        "/projects",
        json={"name": "Files", "slug": "files"},
    ).json()["project"]
    project_id = project["id"]
    workspace_path = Path(unquote(urlparse(project["workspace_uri"]).path))

    upload = persistence_client.post(
        f"/projects/{project_id}/files/upload",
        data={"path": "docs"},
        files={"files": ("readme.md", b"# Hello\n", "text/markdown")},
    )
    make_folder = persistence_client.post(
        f"/projects/{project_id}/files/folder",
        json={"path": "data/raw"},
    )
    root_list = persistence_client.get(f"/projects/{project_id}/files")
    docs_list = persistence_client.get(f"/projects/{project_id}/files", params={"path": "docs"})
    tree_list = persistence_client.get(f"/projects/{project_id}/files", params={"tree": "true"})
    preview = persistence_client.get(
        f"/projects/{project_id}/files/preview",
        params={"path": "docs/readme.md"},
    )

    assert upload.status_code == 200
    assert upload.json()["files"][0]["path"] == "docs/readme.md"
    assert (workspace_path / "docs" / "readme.md").read_text(encoding="utf-8") == "# Hello\n"
    assert make_folder.status_code == 200
    assert (workspace_path / "data" / "raw").is_dir()
    assert root_list.status_code == 200
    root_paths = {item["path"]: item for item in root_list.json()["files"]}
    assert root_paths["docs"]["kind"] == "directory"
    assert root_paths["data"]["kind"] == "directory"
    assert docs_list.status_code == 200
    docs_file = docs_list.json()["files"][0]
    assert docs_file["path"] == "docs/readme.md"
    assert docs_file["kind"] == "file"
    assert docs_file["previewable"] is True
    assert docs_file["preview_url"].endswith("/files/preview?path=docs/readme.md")
    assert docs_file["download_url"].endswith("/files/download?path=docs/readme.md")
    assert tree_list.status_code == 200
    tree_paths = {item["path"]: item for item in tree_list.json()["tree"]}
    assert tree_paths["docs"]["children"][0]["path"] == "docs/readme.md"
    assert tree_paths["data"]["children"][0]["path"] == "data/raw"
    assert preview.status_code == 200
    assert preview.json()["preview_kind"] == "markdown"
    assert preview.json()["content"] == "# Hello\n"

    rename = persistence_client.patch(
        f"/projects/{project_id}/files",
        json={"path": "docs/readme.md", "new_path": "docs/renamed.md"},
    )
    download = persistence_client.get(
        f"/projects/{project_id}/files/download",
        params={"path": "docs/renamed.md"},
    )
    delete = persistence_client.request(
        "DELETE",
        f"/projects/{project_id}/files",
        json={"path": "docs/renamed.md"},
    )
    final_list = persistence_client.get(f"/projects/{project_id}/files", params={"path": "docs"})

    assert rename.status_code == 200
    assert rename.json()["file"]["path"] == "docs/renamed.md"
    assert not (workspace_path / "docs" / "readme.md").exists()
    assert download.status_code == 200
    assert download.content == b"# Hello\n"
    assert delete.status_code == 200
    assert delete.json()["status"] == "deleted"
    assert final_list.status_code == 200
    assert final_list.json()["files"] == []


def test_api_project_files_onlyoffice_preview_uses_system_config(persistence_client) -> None:
    settings = persistence_client.put(
        "/system/onlyoffice",
        json={
            "enabled": True,
            "document_server_url": "http://onlyoffice.test/",
            "public_api_base": "http://api.test/",
            "jwt_secret": None,
            "lang": "zh-CN",
            "project_file_edit_enabled": True,
            "run_artifact_edit_enabled": False,
        },
    )
    project = persistence_client.post(
        "/projects",
        json={"name": "Office Files", "slug": "office-files"},
    ).json()["project"]
    project_id = project["id"]
    workspace_path = Path(unquote(urlparse(project["workspace_uri"]).path))
    (workspace_path / "docs").mkdir()
    (workspace_path / "docs" / "brief.docx").write_bytes(b"PK\x03\x04 docx bytes")
    (workspace_path / "docs" / "report.pdf").write_bytes(b"%PDF-1.7 pdf bytes")

    listed = persistence_client.get(f"/projects/{project_id}/files", params={"path": "docs"})

    assert settings.status_code == 200
    assert listed.status_code == 200
    files = {item["path"]: item for item in listed.json()["files"]}
    assert isinstance(files["docs/brief.docx"]["version"], str)
    assert files["docs/brief.docx"]["version"]
    assert files["docs/report.pdf"]["onlyoffice_config_url"] is None
    assert files["docs/brief.docx"]["onlyoffice_config_url"] == (
        f"/projects/{project_id}/files/onlyoffice/config?path=docs%2Fbrief.docx"
    )

    config_response = persistence_client.get(files["docs/brief.docx"]["onlyoffice_config_url"])

    assert config_response.status_code == 200
    payload = config_response.json()
    assert payload["document_server_url"] == "http://onlyoffice.test"
    assert payload["script_url"] == "http://onlyoffice.test/web-apps/apps/api/documents/api.js"
    onlyoffice_config = payload["config"]
    assert onlyoffice_config["documentType"] == "word"
    assert onlyoffice_config["document"]["fileType"] == "docx"
    assert onlyoffice_config["document"]["title"] == "brief.docx"
    assert onlyoffice_config["document"]["permissions"]["edit"] is True
    assert onlyoffice_config["editorConfig"]["mode"] == "edit"
    assert onlyoffice_config["editorConfig"]["coEditing"] == {"mode": "strict", "change": False}
    assert onlyoffice_config["editorConfig"]["customization"]["autosave"] is False
    assert onlyoffice_config["editorConfig"]["customization"]["forcesave"] is True
    assert onlyoffice_config["document"]["url"].startswith("http://api.test/onlyoffice/files/")
    assert onlyoffice_config["editorConfig"]["callbackUrl"].startswith("http://api.test/onlyoffice/callback/")
    assert onlyoffice_config["editorConfig"]["lang"] == "zh-CN"
    assert "token" not in onlyoffice_config

    file_response = persistence_client.get(urlsplit(onlyoffice_config["document"]["url"]).path)
    callback_response = persistence_client.post(
        urlsplit(onlyoffice_config["editorConfig"]["callbackUrl"]).path,
        json={"status": 1},
    )
    unsupported_response = persistence_client.get(
        f"/projects/{project_id}/files/onlyoffice/config",
        params={"path": "docs/report.pdf"},
    )

    assert file_response.status_code == 200
    assert file_response.content == b"PK\x03\x04 docx bytes"
    assert callback_response.status_code == 200
    assert callback_response.json() == {"error": 0}
    assert unsupported_response.status_code == 415


def test_api_project_files_onlyoffice_callback_saves_editable_file(persistence_client, monkeypatch) -> None:
    downloads: list[str] = []

    async def fake_download(url: str) -> bytes:
        downloads.append(url)
        return b"edited docx bytes"

    monkeypatch.setattr(api_app, "_download_onlyoffice_callback_file", fake_download, raising=False)
    settings = persistence_client.put(
        "/system/onlyoffice",
        json={
            "enabled": True,
            "document_server_url": "http://onlyoffice.test/",
            "public_api_base": "http://api.test/",
            "project_file_edit_enabled": True,
            "run_artifact_edit_enabled": False,
        },
    )
    project = persistence_client.post(
        "/projects",
        json={"name": "Editable Office Files", "slug": "editable-office-files"},
    ).json()["project"]
    project_id = project["id"]
    workspace_path = Path(unquote(urlparse(project["workspace_uri"]).path))
    (workspace_path / "docs").mkdir()
    target = workspace_path / "docs" / "brief.docx"
    target.write_bytes(b"original docx bytes")

    config_response = persistence_client.get(
        f"/projects/{project_id}/files/onlyoffice/config",
        params={"path": "docs/brief.docx"},
    )
    callback_url = config_response.json()["config"]["editorConfig"]["callbackUrl"]

    close_response = persistence_client.post(
        urlsplit(callback_url).path,
        json={"status": 2, "url": "http://onlyoffice.test/edited.docx"},
    )
    callback_response = persistence_client.post(
        urlsplit(callback_url).path,
        json={"status": 6, "forcesavetype": 1, "url": "http://onlyoffice.test/edited.docx"},
    )

    assert settings.status_code == 200
    assert close_response.status_code == 200
    assert close_response.json() == {"error": 0}
    assert callback_response.status_code == 200
    assert callback_response.json() == {"error": 0}
    assert downloads == ["http://onlyoffice.test/edited.docx"]
    assert target.read_bytes() == b"edited docx bytes"


def test_api_project_file_management_rejects_workspace_escape(persistence_client, tmp_path: Path) -> None:
    project = persistence_client.post(
        "/projects",
        json={"name": "Files", "slug": "files"},
    ).json()["project"]
    project_id = project["id"]
    workspace_path = Path(unquote(urlparse(project["workspace_uri"]).path))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace_path / "link").symlink_to(outside)
    (workspace_path / "loop").symlink_to(workspace_path, target_is_directory=True)

    escaped_list = persistence_client.get(f"/projects/{project_id}/files", params={"path": "../outside"})
    tree_list = persistence_client.get(f"/projects/{project_id}/files", params={"tree": "true"})
    escaped_folder = persistence_client.post(
        f"/projects/{project_id}/files/folder",
        json={"path": "../outside"},
    )
    escaped_rename = persistence_client.patch(
        f"/projects/{project_id}/files",
        json={"path": "missing.txt", "new_path": "../outside.txt"},
    )
    symlink_preview = persistence_client.get(
        f"/projects/{project_id}/files/preview",
        params={"path": "link/secret.txt"},
    )

    assert escaped_list.status_code == 400
    assert tree_list.status_code == 200
    tree_paths = {item["path"]: item for item in tree_list.json()["tree"]}
    assert "link" not in tree_paths
    assert tree_paths["loop"]["children"] == []
    assert escaped_folder.status_code == 400
    assert escaped_rename.status_code == 400
    assert symlink_preview.status_code == 400
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "secret"


def test_api_project_file_move_rejects_directory_descendant_target(persistence_client) -> None:
    project = persistence_client.post("/projects", json={"name": "Demo", "slug": "demo"}).json()["project"]
    workspace_path = Path(unquote(urlparse(project["workspace_uri"]).path))
    (workspace_path / "docs").mkdir()

    response = persistence_client.patch(
        f"/projects/{project['id']}/files",
        json={"path": "docs", "new_path": "docs/archive/moved"},
    )

    assert response.status_code == 400
    assert "descendant" in response.json()["detail"]
    assert (workspace_path / "docs").is_dir()


def test_api_creates_and_lists_project_conversations(persistence_client) -> None:
    project = persistence_client.post("/projects", json={"name": "Demo", "slug": "demo"}).json()["project"]
    project_id = project["id"]

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
    assert conversation["workspace_uri"] == project["workspace_uri"]
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["conversations"]] == [conversation["id"]]


def test_api_creates_and_lists_standalone_conversations(persistence_client) -> None:
    project = persistence_client.post("/projects", json={"name": "Demo", "slug": "demo"}).json()["project"]
    project_conversation = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "Project chat"},
    ).json()["conversation"]
    response = persistence_client.post("/conversations", json={"title": "Inbox chat"})
    listed = persistence_client.get("/conversations")

    assert response.status_code == 200
    conversation = response.json()["conversation"]
    workspace_path = Path(unquote(urlparse(conversation["workspace_uri"]).path))
    assert conversation["id"].startswith("conv_")
    assert conversation["project_id"] is None
    assert conversation["owner_user_id"] == "default"
    assert conversation["title"] == "Inbox chat"
    assert workspace_path.is_dir()
    assert workspace_path.name == "workspace"
    assert workspace_path.parent.name == conversation["id"]
    assert project_conversation["project_id"] == project["id"]
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["conversations"]] == [conversation["id"]]


def test_api_renames_standalone_dynamic_conversation(persistence_client) -> None:
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Original Dynamic", "kind": "dynamic_dag"},
    ).json()["conversation"]

    response = persistence_client.patch(
        f"/conversations/{conversation['id']}",
        json={"title": "Renamed Dynamic"},
    )
    listed = persistence_client.get("/conversations", params={"kind": "dynamic_dag"})

    assert response.status_code == 200
    assert response.json()["conversation"]["title"] == "Renamed Dynamic"
    assert [item["id"] for item in listed.json()["conversations"]] == [conversation["id"]]


def test_api_renames_project_dynamic_conversation(persistence_client) -> None:
    project = persistence_client.post(
        "/projects",
        json={"name": "Demo", "slug": "demo"},
    ).json()["project"]
    conversation = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "Original Dynamic", "kind": "dynamic_dag"},
    ).json()["conversation"]

    response = persistence_client.patch(
        f"/projects/{project['id']}/conversations/{conversation['id']}",
        json={"title": "Project Dynamic"},
    )
    listed = persistence_client.get(
        f"/projects/{project['id']}/conversations",
        params={"kind": "dynamic_dag"},
    )

    assert response.status_code == 200
    assert response.json()["conversation"]["title"] == "Project Dynamic"
    assert [item["id"] for item in listed.json()["conversations"]] == [conversation["id"]]


def test_api_lists_standalone_conversation_runs(persistence_client) -> None:
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Dynamic", "kind": "dynamic_dag"},
    ).json()["conversation"]
    state.get_store().create_run(
        run_id="run_dynamic",
        project_id=None,
        conversation_id=conversation["id"],
        user_id="default",
        kind="dynamic_dag",
        status="completed",
        workspace_uri=conversation["workspace_uri"],
    )

    response = persistence_client.get(f"/conversations/{conversation['id']}/runs")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["runs"]] == ["run_dynamic"]


def test_api_deletes_run_database_records_and_run_workspace(persistence_client) -> None:
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Dynamic", "kind": "dynamic_dag"},
    ).json()["conversation"]
    conversation_workspace = Path(unquote(urlparse(conversation["workspace_uri"]).path))
    run_workspace = conversation_workspace / "runs" / "dag_run_delete_one"
    run_workspace.mkdir(parents=True)
    (run_workspace / "artifact.txt").write_text("delete me", encoding="utf-8")
    run_state = RunState(
        run_id="dag_run_delete_one",
        kind="dynamic_dag",
        status="completed",
        workspace_path=str(run_workspace),
        trace=RunTrace(
            run_id="dag_run_delete_one",
            root=RunTraceNode.run(run_id="dag_run_delete_one", status="completed"),
            status="completed",
        ),
    )
    store = state.get_store()
    store.create_run(
        run_id=run_state.run_id,
        project_id=None,
        conversation_id=conversation["id"],
        user_id="user_123",
        kind="dynamic_dag",
        status="completed",
        workspace_uri=conversation["workspace_uri"],
    )
    store.create_run_stream(
        stream_id="stream_delete_one",
        run_id=run_state.run_id,
        project_id=None,
        conversation_id=conversation["id"],
        user_id="user_123",
        kind="dynamic_dag",
        status="completed",
    )
    store.append_run_event(
        run_id=run_state.run_id,
        stream_id="stream_delete_one",
        event_type="run.started",
        payload_json='{"type":"run.started"}',
    )
    store.save_run_state(run_state.run_id, run_state.model_dump_json(), output_text="done")
    store.upsert_review(
        review_id="review_delete_one",
        run_id=run_state.run_id,
        project_id=None,
        kind="dag_review",
    )
    store.append_conversation_message(
        message_id="msg_delete_one",
        conversation_id=conversation["id"],
        project_id=None,
        role="assistant",
        run_id=run_state.run_id,
        status="completed",
        content="done",
        timeline_json='[{"type":"text","content":"done"}]',
    )

    response = persistence_client.delete(f"/runs/{run_state.run_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert persistence_client.get(f"/runs/{run_state.run_id}").status_code == 404
    assert persistence_client.get(f"/runs/{run_state.run_id}/events").status_code == 404
    assert persistence_client.get(f"/runs/{run_state.run_id}/artifacts").status_code == 404
    assert store.get_run(run_state.run_id) is None
    assert store.list_run_events(run_state.run_id) == []
    assert store.list_run_streams(run_state.run_id) == []
    assert store.get_review("review_delete_one") is None
    assert store.list_conversation_messages(conversation["id"]) == []
    assert store.get_conversation(conversation["id"]).last_run_id is None
    assert conversation_workspace.exists()
    assert not run_workspace.exists()


def test_api_lists_orchestration_session_runs(persistence_client) -> None:
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Dynamic", "kind": "dynamic_dag"},
    ).json()["conversation"]
    session = persistence_client.post(
        "/orchestration-sessions",
        json={"conversation_id": conversation["id"], "kind": "dynamic_dag"},
    ).json()["session"]
    state.get_store().create_run(
        run_id="run_dynamic",
        project_id=None,
        conversation_id=conversation["id"],
        user_id="default",
        kind="dynamic_dag",
        status="completed",
        workspace_uri=conversation["workspace_uri"],
    )

    response = persistence_client.get(f"/orchestration-sessions/{session['id']}/runs")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["runs"]] == ["run_dynamic"]


def test_api_lists_saved_dag_runs(persistence_client) -> None:
    saved = persistence_client.post(
        "/saved-dags",
        json={
            "name": "Executable",
            "spec": {
                "id": "executable",
                "name": "Executable",
                "nodes": [
                    {
                        "id": "write",
                        "target": "tool.write_file",
                        "inputs": {"path": "reports/summary.md", "content": "hello"},
                        "boundary": {"allowed_paths": ["."]},
                    }
                ],
                "edges": [],
            },
        },
    ).json()["saved_dag"]
    state.get_store().create_run(
        run_id="run_static",
        project_id=None,
        conversation_id=None,
        user_id="default",
        kind="static_dag",
        status="completed",
        workspace_uri="file:///tmp/workspace",
        saved_dag_id=saved["id"],
    )

    response = persistence_client.get(f"/saved-dags/{saved['id']}/runs")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["runs"]] == ["run_static"]


def test_api_rejects_conversation_for_missing_project(persistence_client) -> None:
    response = persistence_client.post(
        "/projects/proj_missing/conversations",
        json={"title": "No project"},
    )

    assert response.status_code == 404


def test_api_project_message_stream_uses_project_workspace(
    persistence_client,
) -> None:
    state.runner = Runner(
        provider=MockProvider([
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="tool_write_file",
                        arguments={"path": "notes/project-chat.txt", "content": "project"},
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
        json={"title": "Project chat"},
    ).json()["conversation"]
    project_workspace = Path(unquote(urlparse(project["workspace_uri"]).path))
    conversation_workspace = Path(unquote(urlparse(conversation["workspace_uri"]).path))

    response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "write project note",
            "target": "tool",
            "capability_ids": ["tool.write_file"],
            "project_id": project["id"],
            "conversation_id": conversation["id"],
        },
    )
    result = _sse_events(response.text)[-1]["data"]["result"]

    assert response.status_code == 200
    assert conversation_workspace == project_workspace
    assert result["state"]["workspace_path"] == str(project_workspace)
    assert (project_workspace / "notes" / "project-chat.txt").read_text(encoding="utf-8") == "project"


def test_api_standalone_message_stream_uses_conversation_workspace(
    persistence_client,
) -> None:
    state.runner = Runner(
        provider=MockProvider([
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="tool_write_file",
                        arguments={"path": "notes/inbox.txt", "content": "inbox"},
                    )
                ]
            ),
            ChatResponse(content="done"),
        ])
    )
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Inbox chat"},
    ).json()["conversation"]
    conversation_workspace = Path(unquote(urlparse(conversation["workspace_uri"]).path))

    response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "write inbox note",
            "target": "tool",
            "capability_ids": ["tool.write_file"],
            "conversation_id": conversation["id"],
        },
    )
    result = _sse_events(response.text)[-1]["data"]["result"]
    run_id = result["state"]["run_id"]
    persisted_run = state.get_store().get_run(run_id)

    assert response.status_code == 200
    assert result["state"]["workspace_path"] == str(conversation_workspace)
    assert (conversation_workspace / "notes" / "inbox.txt").read_text(encoding="utf-8") == "inbox"
    assert persisted_run is not None
    assert persisted_run.project_id is None
    assert persisted_run.conversation_id == conversation["id"]


def test_api_deletes_conversation_runs_and_per_run_workspace(persistence_client, tmp_path: Path) -> None:
    project = persistence_client.post(
        "/projects",
        json={"name": "Demo", "slug": "demo"},
    ).json()["project"]
    conversation = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "Old chat"},
    ).json()["conversation"]
    run_workspace = tmp_path / ".dagent" / "runs" / "tool_run_delete"
    run_workspace.mkdir(parents=True)
    (run_workspace / "scratch.txt").write_text("remove me", encoding="utf-8")
    run_state = RunState(
        run_id="tool_run_delete",
        kind="tool",
        status="awaiting_review",
        workspace_path=str(run_workspace),
        trace=RunTrace(
            run_id="tool_run_delete",
            root=RunTraceNode.run(run_id="tool_run_delete", status="awaiting_review"),
            status="awaiting_review",
        ),
    )
    store = state.get_store()
    store.create_run(
        run_id=run_state.run_id,
        project_id=project["id"],
        conversation_id=conversation["id"],
        user_id="user_123",
        kind="tool",
        status="awaiting_review",
        workspace_uri=project["workspace_uri"],
    )
    store.create_run_stream(
        stream_id="stream_delete",
        run_id=run_state.run_id,
        project_id=project["id"],
        conversation_id=conversation["id"],
        user_id="user_123",
        kind="tool",
        status="awaiting_review",
    )
    store.append_run_event(
        run_id=run_state.run_id,
        stream_id="stream_delete",
        event_type="run.started",
        payload_json='{"type":"run.started"}',
    )
    store.save_run_state(run_state.run_id, run_state.model_dump_json(), output_text="waiting")
    store.upsert_review(
        review_id="review_delete",
        run_id=run_state.run_id,
        project_id=project["id"],
        kind="capability_review",
    )

    response = persistence_client.delete(
        f"/projects/{project['id']}/conversations/{conversation['id']}"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert persistence_client.get(
        f"/projects/{project['id']}/conversations/{conversation['id']}"
    ).status_code == 404
    assert persistence_client.get(f"/runs/{run_state.run_id}").status_code == 404
    assert store.get_run(run_state.run_id) is None
    assert store.list_run_events(run_state.run_id) == []
    assert store.get_review("review_delete") is None
    assert not run_workspace.exists()


def test_api_deletes_project_run_files_and_saved_dag_artifacts(
    persistence_client,
    tmp_path: Path,
) -> None:
    project = persistence_client.post(
        "/projects",
        json={"name": "Demo", "slug": "demo"},
    ).json()["project"]
    conversation = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "Project chat"},
    ).json()["conversation"]
    run_workspace = tmp_path / ".dagent" / "runs" / "tool_run_project_delete"
    run_workspace.mkdir(parents=True)
    (run_workspace / "scratch.txt").write_text("remove me", encoding="utf-8")
    run_state = RunState(
        run_id="tool_run_project_delete",
        kind="tool",
        status="completed",
        workspace_path=str(run_workspace),
        trace=RunTrace(
            run_id="tool_run_project_delete",
            root=RunTraceNode.run(run_id="tool_run_project_delete", status="completed"),
            status="completed",
        ),
    )
    spec = {
        "id": "project_upload",
        "name": "Project Upload",
        "artifacts": {
            "source": {
                "id": "source",
                "paths": ["uploads/source.txt"],
                "description": "source",
            }
        },
        "nodes": [
            {
                "id": "read",
                "target": "tool.read_file",
                "inputs": {"path": "uploads/source.txt"},
                "artifact_inputs": ["source"],
                "boundary": {"allowed_paths": ["uploads/source.txt"]},
            }
        ],
        "edges": [],
    }
    store = state.get_store()
    store.create_run(
        run_id=run_state.run_id,
        project_id=project["id"],
        conversation_id=conversation["id"],
        user_id="user_123",
        kind="tool",
        status="completed",
        workspace_uri=project["workspace_uri"],
    )
    store.save_run_state(run_state.run_id, run_state.model_dump_json(), output_text="done")
    saved = persistence_client.post(
        "/saved-dags",
        json={"project_id": project["id"], "name": "Project Upload", "spec": spec},
    ).json()["saved_dag"]
    upload = persistence_client.post(
        f"/saved-dags/{saved['id']}/artifacts/source/upload",
        files={"files": ("source.txt", b"hello", "text/plain")},
    )
    artifact_root = api_app._saved_dag_artifact_root(saved["id"])

    assert upload.status_code == 200
    assert artifact_root.exists()

    response = persistence_client.delete(f"/projects/{project['id']}")

    assert response.status_code == 200
    assert store.get_project(project["id"]) is None
    assert store.get_run(run_state.run_id) is None
    assert store.get_saved_dag(saved["id"]) is None
    assert not run_workspace.exists()
    assert not artifact_root.exists()


def test_api_deletes_conversation_without_removing_project_workspace(persistence_client) -> None:
    project = persistence_client.post(
        "/projects",
        json={"name": "Demo", "slug": "demo"},
    ).json()["project"]
    conversation = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "Shared workspace chat"},
    ).json()["conversation"]
    project_workspace = Path(unquote(urlparse(project["workspace_uri"]).path))
    (project_workspace / "notes.txt").write_text("keep me", encoding="utf-8")
    run_state = RunState(
        run_id="tool_run_shared_workspace",
        kind="tool",
        status="completed",
        workspace_path=str(project_workspace),
        trace=RunTrace(
            run_id="tool_run_shared_workspace",
            root=RunTraceNode.run(run_id="tool_run_shared_workspace", status="completed"),
            status="completed",
        ),
    )
    store = state.get_store()
    store.create_run(
        run_id=run_state.run_id,
        project_id=project["id"],
        conversation_id=conversation["id"],
        user_id="user_123",
        kind="tool",
        status="completed",
        workspace_uri=project["workspace_uri"],
    )
    store.save_run_state(run_state.run_id, run_state.model_dump_json(), output_text="done")

    response = persistence_client.delete(
        f"/projects/{project['id']}/conversations/{conversation['id']}"
    )

    assert response.status_code == 200
    assert project_workspace.is_dir()
    assert (project_workspace / "notes.txt").read_text(encoding="utf-8") == "keep me"


def test_api_deletes_completed_answerless_conversation_with_stale_lock(persistence_client) -> None:
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Answerless tool chat"},
    ).json()["conversation"]
    conversation_workspace = Path(unquote(urlparse(conversation["workspace_uri"]).path))
    (conversation_workspace / "scratch.txt").write_text("remove me", encoding="utf-8")
    run_state = RunState(
        run_id="tool_run_answerless_delete",
        kind="tool",
        status="completed",
        workspace_path=str(conversation_workspace),
        user_request="list files",
        trace=RunTrace(
            run_id="tool_run_answerless_delete",
            root=RunTraceNode.run(run_id="tool_run_answerless_delete", status="completed"),
            status="completed",
        ),
    )
    store = state.get_store()
    store.create_run(
        run_id=run_state.run_id,
        project_id=None,
        conversation_id=conversation["id"],
        user_id="user_123",
        kind="tool",
        status="completed",
        workspace_uri=conversation["workspace_uri"],
    )
    store.create_run_stream(
        stream_id="stream_answerless_stale",
        run_id=run_state.run_id,
        project_id=None,
        conversation_id=conversation["id"],
        user_id="user_123",
        kind="tool",
        status="completed",
    )
    store.save_run_state(run_state.run_id, run_state.model_dump_json(), output_text="")
    stale_lock = store.acquire_conversation_lock(conversation["id"], owner="stale_stream")

    response = persistence_client.delete(f"/conversations/{conversation['id']}")

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert store.get_conversation(conversation["id"]) is None
    assert store.get_run(run_state.run_id) is None
    assert store.list_run_events(run_state.run_id) == []
    assert not conversation_workspace.exists()
    stale_lock.release()


def test_api_deletes_standalone_conversation_workspace(persistence_client) -> None:
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Inbox chat"},
    ).json()["conversation"]
    conversation_workspace = Path(unquote(urlparse(conversation["workspace_uri"]).path))
    conversation_root = conversation_workspace.parent
    (conversation_workspace / "notes.txt").write_text("remove me", encoding="utf-8")
    run_state = RunState(
        run_id="tool_run_standalone_delete",
        kind="tool",
        status="completed",
        workspace_path=str(conversation_workspace),
        trace=RunTrace(
            run_id="tool_run_standalone_delete",
            root=RunTraceNode.run(run_id="tool_run_standalone_delete", status="completed"),
            status="completed",
        ),
    )
    store = state.get_store()
    store.create_run(
        run_id=run_state.run_id,
        project_id=None,
        conversation_id=conversation["id"],
        user_id="user_123",
        kind="tool",
        status="completed",
        workspace_uri=conversation["workspace_uri"],
    )
    store.save_run_state(run_state.run_id, run_state.model_dump_json(), output_text="done")

    response = persistence_client.delete(f"/conversations/{conversation['id']}")

    assert response.status_code == 200
    assert store.get_run(run_state.run_id) is None
    assert not conversation_workspace.exists()
    assert not conversation_root.exists()


def test_api_deletes_empty_standalone_conversation_workspace(persistence_client) -> None:
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Interrupted chat"},
    ).json()["conversation"]
    conversation_workspace = Path(unquote(urlparse(conversation["workspace_uri"]).path))
    conversation_root = conversation_workspace.parent

    response = persistence_client.delete(f"/conversations/{conversation['id']}")

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert state.get_store().get_conversation(conversation["id"]) is None
    assert not conversation_workspace.exists()
    assert not conversation_root.exists()


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
    project_workspace_path = Path(unquote(urlparse(project["workspace_uri"]).path))
    workspace_path = Path(unquote(urlparse(conversation["workspace_uri"]).path))

    response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "write the note",
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
    assert workspace_path == project_workspace_path
    assert (workspace_path / "notes" / "shared.txt").read_text(encoding="utf-8") == "hello"
    assert not (workspace_path / run_id).exists()
    persisted_run = store.get_run(run_id)
    persisted_state = store.get_run_state(run_id)
    persisted_events = store.list_run_events(run_id)
    persisted_streams = store.list_run_streams(run_id)
    listed_runs = persistence_client.get(
        f"/projects/{project['id']}/conversations/{conversation['id']}/runs"
    )
    run_response = persistence_client.get(f"/runs/{run_id}")
    replay_response = persistence_client.get(f"/runs/{run_id}/events", params={"after_event_id": 1})
    messages_response = persistence_client.get(
        f"/projects/{project['id']}/conversations/{conversation['id']}/messages"
    )

    assert persisted_run is not None
    assert persisted_run.project_id == project["id"]
    assert persisted_run.conversation_id == conversation["id"]
    assert persisted_run.status == "completed"
    assert persisted_run.output_text == "done"
    assert persisted_state is not None
    assert persisted_state.workspace_path == str(workspace_path)
    assert len(persisted_events) == len(events)
    assert [event.event_id for event in persisted_events] == list(range(1, len(events) + 1))
    assert len(persisted_streams) == 1
    assert persisted_streams[0].status == "completed"
    assert persisted_streams[0].completed_at is not None
    assert listed_runs.status_code == 200
    assert [item["id"] for item in listed_runs.json()["runs"]] == [run_id]
    assert run_response.status_code == 200
    assert run_response.json()["run"]["id"] == run_id
    assert "state_json" not in run_response.json()["run"]
    assert "error_json" not in run_response.json()["run"]
    assert run_response.json()["run"]["has_state"] is True
    assert run_response.json()["run"]["has_error"] is False
    assert replay_response.status_code == 200
    assert [event["event_id"] for event in replay_response.json()["events"]] == list(range(2, len(events) + 1))
    assert replay_response.json()["events"][0]["payload"]["sequence"] == 2
    assert messages_response.status_code == 200
    persisted_messages = messages_response.json()["messages"]
    assert [message["role"] for message in persisted_messages] == ["user", "assistant"]
    assert persisted_messages[0]["content"] == "write the note"
    assert persisted_messages[0]["run_id"] == run_id
    assert persisted_messages[1]["content"] == "done"
    assert persisted_messages[1]["run_id"] == run_id
    assert persisted_messages[1]["status"] == "completed"
    capability_items = [
        item for item in persisted_messages[1]["timeline"]
        if item["type"] == "capability"
    ]
    assert len(capability_items) == 1
    assert capability_items[0]["status"] == "completed"
    assert capability_items[0]["result"]["type"] == "capability.call.completed"

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

    delete_response = persistence_client.delete(f"/runs/{run_id}")
    messages_after_delete = persistence_client.get(
        f"/projects/{project['id']}/conversations/{conversation['id']}/messages"
    )

    assert delete_response.status_code == 200
    assert messages_after_delete.status_code == 200
    assert messages_after_delete.json()["messages"] == []


def test_api_run_summary_hides_error_json_and_reports_error_flag(
    persistence_client,
) -> None:
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Failed run"},
    ).json()["conversation"]
    store = state.get_store()
    store.create_run(
        run_id="run_failed_summary",
        project_id=None,
        conversation_id=conversation["id"],
        user_id="user_123",
        kind="tool",
        status="running",
        workspace_uri=conversation["workspace_uri"],
    )
    store.save_run_error("run_failed_summary", json.dumps({"message": "boom"}))

    response = persistence_client.get("/runs/run_failed_summary")

    assert response.status_code == 200
    run = response.json()["run"]
    assert run["id"] == "run_failed_summary"
    assert "state_json" not in run
    assert "error_json" not in run
    assert run["has_state"] is False
    assert run["has_error"] is True


def test_api_message_stream_failure_before_run_started_updates_conversation_message(
    persistence_client,
) -> None:
    class FailingRunner:
        enable_validation = False

        async def stream(self, *args, **kwargs):
            raise RuntimeError("provider unavailable")
            yield

        def run_state(self, run_id: str):
            return None

        def close(self) -> None:
            pass

    state.runner = FailingRunner()
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Failing chat"},
    ).json()["conversation"]

    response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "please fail",
            "target": "tool",
            "conversation_id": conversation["id"],
        },
    )
    events = _sse_events(response.text)
    messages_response = persistence_client.get(f"/conversations/{conversation['id']}/messages")

    assert response.status_code == 200
    assert events[-1]["type"] == "run.failed"
    assert messages_response.status_code == 200
    messages = messages_response.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["status"] == "failed"
    assert "provider unavailable" in messages[1]["content"]
    assert messages[1]["timeline"][-1]["type"] == "text"


def test_api_run_trace_does_not_hide_store_read_errors(persistence_client, monkeypatch) -> None:
    store = state.get_store()

    def fail_get_run_state(run_id: str) -> RunState | None:
        raise RuntimeError(f"storage unavailable for {run_id}")

    monkeypatch.setattr(store, "get_run_state", fail_get_run_state)

    with pytest.raises(RuntimeError, match="storage unavailable"):
        persistence_client.get("/runs/run_missing/trace")


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
            "input": "hi",
            "project_id": project["id"],
            "conversation_id": conversation["id"],
            "workspace_root": "custom",
        },
    )

    assert response.status_code == 400
    assert "workspace_root" in response.json()["detail"]


def test_api_project_message_stream_requires_project_id_for_project_conversation(persistence_client) -> None:
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
            "input": "hi",
            "target": "tool",
            "conversation_id": conversation["id"],
        },
    )

    assert response.status_code == 400
    assert "project_id" in response.json()["detail"]


def test_api_message_stream_rejects_orchestration_conversation(persistence_client) -> None:
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Dynamic DAG", "kind": "dynamic_dag"},
    ).json()["conversation"]

    response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "hello",
            "target": "auto",
            "conversation_id": conversation["id"],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Chat streams require a chat conversation."


def test_api_unscoped_delete_rejects_project_conversation(persistence_client) -> None:
    project = persistence_client.post(
        "/projects",
        json={"name": "Demo", "slug": "demo"},
    ).json()["project"]
    conversation = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "First chat"},
    ).json()["conversation"]

    response = persistence_client.delete(f"/conversations/{conversation['id']}")

    assert response.status_code == 400
    assert "project" in response.json()["detail"]
    assert persistence_client.get(
        f"/projects/{project['id']}/conversations/{conversation['id']}"
    ).status_code == 200


def test_api_unscoped_review_resume_rejects_project_review(persistence_client) -> None:
    project = persistence_client.post(
        "/projects",
        json={"name": "Demo", "slug": "demo"},
    ).json()["project"]
    conversation = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "First chat"},
    ).json()["conversation"]
    store = state.get_store()
    store.create_run(
        run_id="tool_run_project_review",
        project_id=project["id"],
        conversation_id=conversation["id"],
        user_id="user_123",
        kind="tool",
        status="awaiting_review",
        workspace_uri=project["workspace_uri"],
    )
    store.upsert_review(
        review_id="review_project",
        run_id="tool_run_project_review",
        project_id=project["id"],
        kind="capability_review",
    )

    response = persistence_client.post("/reviews/review_project/resume", json={"approved": False})

    assert response.status_code == 400
    assert "project" in response.json()["detail"]


def test_api_project_delete_rejects_active_conversation(persistence_client) -> None:
    project = persistence_client.post(
        "/projects",
        json={"name": "Demo", "slug": "demo"},
    ).json()["project"]
    conversation = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "Busy chat"},
    ).json()["conversation"]
    workspace_path = Path(unquote(urlparse(project["workspace_uri"]).path))
    lock = state.get_store().acquire_conversation_lock(conversation["id"], owner="manual")
    try:
        response = persistence_client.delete(f"/projects/{project['id']}")
    finally:
        lock.release()

    assert response.status_code == 409
    assert persistence_client.get(f"/projects/{project['id']}").status_code == 200
    assert workspace_path.is_dir()


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
                "input": "hi",
                "target": "tool",
                "project_id": project["id"],
                "conversation_id": first["id"],
            },
        )
        other_conversation = persistence_client.post(
            "/messages/stream",
            json={
                "input": "hi",
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


def test_api_project_message_stream_continues_from_persisted_conversation(
    persistence_client,
) -> None:
    state.runner = Runner(provider=MockProvider([
        ChatResponse(content="first done"),
        ChatResponse(content="second done"),
    ]))
    project = persistence_client.post(
        "/projects",
        json={"name": "Demo", "slug": "demo"},
    ).json()["project"]
    conversation = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "First chat"},
    ).json()["conversation"]

    first_response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "first",
            "target": "tool",
            "project_id": project["id"],
            "conversation_id": conversation["id"],
        },
    )
    second_response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "second",
            "target": "tool",
            "project_id": project["id"],
            "conversation_id": conversation["id"],
        },
    )
    first_result = _sse_events(first_response.text)[-1]["data"]["result"]
    second_result = _sse_events(second_response.text)[-1]["data"]["result"]

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_result["state"]["run_id"] != first_result["state"]["run_id"]
    assert second_result["output_text"] == "second done"
    assert (
        state.get_store().get_run(first_result["state"]["run_id"]).output_text
        == "first done"
    )
    assert (
        state.get_store().get_run(second_result["state"]["run_id"]).output_text
        == "second done"
    )
    assert [
        message["role"]
        for message in state.get_runner().runtime.provider.requests[1]["messages"]
    ] == ["system", "user", "assistant", "user"]


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
            "input": "read outside",
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
    messages_response = persistence_client.get(
        f"/projects/{project['id']}/conversations/{conversation['id']}/messages"
    )

    assert resume_response.status_code == 200
    assert resume_result["state"]["run_id"] == run_id
    assert resume_result["state"]["status"] == "completed"
    assert resume_result["output_text"] == "I will stop."
    assert review is not None
    assert review.status == "resolved"
    assert run_state is not None
    assert run_state.status == "completed"
    assert messages_response.status_code == 200
    messages = messages_response.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "I will stop."
    assert messages[1]["status"] == "completed"
    capability_items = [
        item for item in messages[1]["timeline"]
        if item["type"] == "capability"
    ]
    assert len(capability_items) == 1
    assert capability_items[0]["status"] == "rejected"
    assert capability_items[0]["result"]["type"] == "capability.call.failed"


def test_api_standalone_review_resume_uses_db_state_after_runner_restart(
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
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Inbox chat"},
    ).json()["conversation"]
    workspace_path = Path(unquote(urlparse(conversation["workspace_uri"]).path))
    blocked_dir = workspace_path.parent / "blocked"
    blocked_dir.mkdir(parents=True)
    (blocked_dir / "secret.txt").write_text("private", encoding="utf-8")

    stream_response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "read outside",
            "target": "tool",
            "capability_ids": ["tool.read_file"],
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
        f"/reviews/{review_id}/resume",
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


def test_api_saved_dag_crud_persists_static_dag_spec(persistence_client) -> None:
    project = persistence_client.post(
        "/projects",
        json={"name": "DAGs", "slug": "dags"},
    ).json()["project"]
    spec = {
        "id": "project_report",
        "name": "Project Report",
        "nodes": [
            {
                "id": "write",
                "target": "tool.write_file",
                "inputs": {"path": "reports/summary.md", "content": "hello"},
                "boundary": {"allowed_paths": ["."]},
            }
        ],
        "edges": [],
    }

    created = persistence_client.post(
        "/saved-dags",
        json={
            "project_id": project["id"],
            "name": "Project Report",
            "description": "first",
            "spec": spec,
            "layout": {"nodes": [{"id": "write", "x": 10, "y": 20}]},
        },
    )
    saved = created.json()["saved_dag"]
    listed = persistence_client.get("/saved-dags", params={"project_id": project["id"]})
    fetched = persistence_client.get(f"/saved-dags/{saved['id']}")
    updated = persistence_client.patch(
        f"/saved-dags/{saved['id']}",
        json={
            "name": "Project Report v2",
            "description": "second",
            "spec": {**spec, "name": "Project Report v2"},
            "layout": {"nodes": [{"id": "write", "x": 30, "y": 40}]},
            "expected_revision": saved["revision"],
        },
    )
    archived = persistence_client.delete(f"/saved-dags/{saved['id']}")
    listed_after_delete = persistence_client.get("/saved-dags", params={"project_id": project["id"]})

    assert created.status_code == 200
    assert saved["id"].startswith("dag_")
    assert saved["project_id"] == project["id"]
    assert saved["spec"]["id"] == spec["id"]
    assert saved["spec"]["name"] == spec["name"]
    assert saved["spec"]["nodes"][0]["id"] == "write"
    assert saved["spec"]["nodes"][0]["target"] == "tool.write_file"
    assert saved["spec"]["nodes"][0]["inputs"] == {"path": "reports/summary.md", "content": "hello"}
    assert saved["layout"] == {"nodes": [{"id": "write", "x": 10, "y": 20}]}
    assert saved["revision"] == 1
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["saved_dags"]] == [saved["id"]]
    assert fetched.status_code == 200
    assert fetched.json()["saved_dag"]["spec"]["id"] == spec["id"]
    assert updated.status_code == 200
    assert updated.json()["saved_dag"]["name"] == "Project Report v2"
    assert updated.json()["saved_dag"]["revision"] == 2
    assert archived.status_code == 200
    assert listed_after_delete.json()["saved_dags"] == []


def test_api_saved_dag_payload_falls_back_to_valid_empty_spec(
    persistence_client,
) -> None:
    created = persistence_client.post(
        "/saved-dags",
        json={
            "name": "Corruptible",
            "spec": {
                "id": "corruptible",
                "name": "Corruptible",
                "nodes": [
                    {
                        "id": "write",
                        "target": "tool.write_file",
                        "inputs": {"path": "reports/summary.md", "content": "hello"},
                        "boundary": {"allowed_paths": ["."]},
                    }
                ],
                "edges": [],
            },
        },
    )
    saved = created.json()["saved_dag"]
    store = state.get_store()
    store._conn.execute("UPDATE saved_dags SET spec_json = ? WHERE id = ?", ("{bad json", saved["id"]))
    store._conn.commit()

    response = persistence_client.get(f"/saved-dags/{saved['id']}")
    payload = response.json()["saved_dag"]["spec"]

    assert response.status_code == 200
    assert payload["id"] == saved["id"]
    assert payload["name"] == "Corruptible"
    assert payload["nodes"] == []
    assert payload["edges"] == []


def test_api_saved_dag_stream_uses_conversation_workspace_and_persists_run(
    persistence_client,
) -> None:
    state.runner = Runner(provider=MockProvider([]))
    project = persistence_client.post(
        "/projects",
        json={"name": "Static", "slug": "static"},
    ).json()["project"]
    conversation = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "Static DAG session", "kind": "static_dag"},
    ).json()["conversation"]
    spec = {
        "id": "write_static_note",
        "name": "Write Static Note",
        "nodes": [
            {
                "id": "write",
                "target": "tool.write_file",
                "inputs": {"path": "reports/static.md", "content": "static"},
                "boundary": {"allowed_paths": ["."]},
            }
        ],
        "edges": [],
    }
    saved = persistence_client.post(
        "/saved-dags",
        json={
            "project_id": project["id"],
            "name": "Write Static Note",
            "spec": spec,
        },
    ).json()["saved_dag"]
    workspace_path = Path(unquote(urlparse(project["workspace_uri"]).path))

    response = persistence_client.post(
        f"/saved-dags/{saved['id']}/run/stream",
        json={
            "project_id": project["id"],
            "conversation_id": conversation["id"],
        },
    )
    events = _sse_events(response.text)
    result = events[-1]["data"]["result"]
    run_id = result["state"]["run_id"]
    persisted_run = state.get_store().get_run(run_id)
    persisted_state = state.get_store().get_run_state(run_id)
    listed_runs = persistence_client.get(
        f"/projects/{project['id']}/conversations/{conversation['id']}/runs"
    )

    assert response.status_code == 200
    assert result["state"]["kind"] == "static_dag"
    assert result["state"]["workspace_path"] == str(workspace_path)
    assert (workspace_path / "reports" / "static.md").read_text(encoding="utf-8") == "static"
    assert persisted_run is not None
    assert persisted_run.saved_dag_id == saved["id"]
    assert persisted_run.project_id == project["id"]
    assert persisted_run.conversation_id == conversation["id"]
    assert persisted_run.status == "completed"
    assert persisted_state is not None
    assert persisted_state.kind == "static_dag"
    assert listed_runs.status_code == 200
    assert [item["id"] for item in listed_runs.json()["runs"]] == [run_id]


def test_api_saved_dag_artifact_upload_persists_across_process_state_reset(persistence_client) -> None:
    state.runner = Runner(provider=MockProvider([]))
    spec = {
        "id": "with_upload",
        "name": "With Upload",
        "artifacts": {
            "source": {
                "id": "source",
                "paths": ["uploads/source.txt"],
                "description": "source",
            }
        },
        "nodes": [
            {
                "id": "read",
                "target": "tool.read_file",
                "inputs": {
                    "path": {
                        "$expr": {
                            "type": "artifact",
                            "artifact_id": "source",
                            "field": "absolute_path",
                        }
                    }
                },
                "artifact_inputs": ["source"],
                "boundary": {
                    "allowed_paths": [
                        {
                            "$expr": {
                                "type": "artifact",
                                "artifact_id": "source",
                                "field": "absolute_path",
                            }
                        }
                    ]
                },
            }
        ],
        "edges": [],
    }
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Static DAG session", "kind": "static_dag"},
    ).json()["conversation"]
    saved = persistence_client.post(
        "/saved-dags",
        json={"name": "With Upload", "spec": spec},
    ).json()["saved_dag"]

    response = persistence_client.post(
        f"/saved-dags/{saved['id']}/artifacts/source/upload",
        files={"files": ("source.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200
    assert response.json() == {"artifact_id": "source", "files": ["source.txt"]}

    state.dag_artifact_uploads.clear()
    run_response = persistence_client.post(
        f"/saved-dags/{saved['id']}/run/stream",
        json={"conversation_id": conversation["id"]},
    )
    events = _sse_events(run_response.text)
    result = events[-1]["data"]["result"]
    workspace_path = Path(result["state"]["workspace_path"])

    assert run_response.status_code == 200
    assert (workspace_path / "uploads" / "source.txt").read_text(encoding="utf-8") == "hello"
    assert result["state"]["status"] == "completed"


def test_api_saved_dag_artifact_upload_keeps_previous_file_when_replacement_fails(
    persistence_client,
    monkeypatch,
) -> None:
    spec = {
        "id": "with_upload",
        "name": "With Upload",
        "artifacts": {
            "source": {
                "id": "source",
                "paths": ["uploads/source.txt"],
                "description": "source",
            }
        },
        "nodes": [
            {
                "id": "read",
                "target": "tool.read_file",
                "inputs": {"path": "uploads/source.txt"},
                "artifact_inputs": ["source"],
                "boundary": {"allowed_paths": ["."]},
            }
        ],
        "edges": [],
    }
    saved = persistence_client.post(
        "/saved-dags",
        json={"name": "With Upload", "spec": spec},
    ).json()["saved_dag"]
    first = persistence_client.post(
        f"/saved-dags/{saved['id']}/artifacts/source/upload",
        files={"files": ("source.txt", b"original", "text/plain")},
    )

    def fail_materialize(*args, **kwargs):
        raise api_app.ArtifactPathError("materialize failed")

    monkeypatch.setattr(api_app, "materialize_artifact_uploads", fail_materialize)
    replacement = persistence_client.post(
        f"/saved-dags/{saved['id']}/artifacts/source/upload",
        files={"files": ("source.txt", b"replacement", "text/plain")},
    )
    root = api_app._saved_dag_artifact_root(saved["id"])

    assert first.status_code == 200
    assert replacement.status_code == 400
    assert (root / "uploads" / "source.txt").read_text(encoding="utf-8") == "original"


def test_api_orchestration_session_crud_uses_conversation_kind(persistence_client) -> None:
    project = persistence_client.post(
        "/projects",
        json={"name": "Dynamic", "slug": "dynamic"},
    ).json()["project"]
    conversation_response = persistence_client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "Dynamic DAG session", "kind": "dynamic_dag"},
    )
    conversation = conversation_response.json()["conversation"]
    draft = {"version": 1, "status": "draft", "nodes": [], "edges": []}

    created = persistence_client.post(
        "/orchestration-sessions",
        json={
            "conversation_id": conversation["id"],
            "project_id": project["id"],
            "kind": "dynamic_dag",
            "draft_dag": draft,
            "ui_state": {"selectedNodeId": "answer"},
        },
    )
    session = created.json()["session"]
    fetched = persistence_client.get(f"/orchestration-sessions/{session['id']}")
    by_conversation = persistence_client.get(
        f"/conversations/{conversation['id']}/orchestration-session"
    )
    updated = persistence_client.patch(
        f"/orchestration-sessions/{session['id']}",
        json={
            "draft_dag": {**draft, "status": "review_required"},
            "ui_state": {"selectedNodeId": "review"},
        },
    )

    assert conversation_response.status_code == 200
    assert conversation["kind"] == "dynamic_dag"
    assert created.status_code == 200
    assert session["conversation_id"] == conversation["id"]
    assert session["project_id"] == project["id"]
    assert session["kind"] == "dynamic_dag"
    assert session["draft_dag"] == draft
    assert session["ui_state"] == {"selectedNodeId": "answer"}
    assert fetched.status_code == 200
    assert fetched.json()["session"]["id"] == session["id"]
    assert by_conversation.status_code == 200
    assert by_conversation.json()["session"]["id"] == session["id"]
    assert updated.status_code == 200
    assert updated.json()["session"]["draft_dag"]["status"] == "review_required"
    assert updated.json()["session"]["ui_state"] == {"selectedNodeId": "review"}


def test_api_orchestration_session_requires_matching_conversation_kind(persistence_client) -> None:
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Regular chat"},
    ).json()["conversation"]

    response = persistence_client.post(
        "/orchestration-sessions",
        json={
            "conversation_id": conversation["id"],
            "kind": "dynamic_dag",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Conversation kind does not match orchestration session kind."


def test_api_orchestration_session_patch_clears_nullable_fields(persistence_client) -> None:
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Static DAG", "kind": "static_dag"},
    ).json()["conversation"]
    spec = {
        "id": "clearable_dag",
        "name": "Clearable DAG",
        "nodes": [
            {
                "id": "write",
                "target": "tool.write_file",
                "inputs": {"path": "out.txt", "content": "ok"},
                "boundary": {"allowed_paths": ["."]},
            }
        ],
        "edges": [],
    }
    saved = persistence_client.post(
        "/saved-dags",
        json={"name": "Clearable DAG", "spec": spec},
    ).json()["saved_dag"]
    session = persistence_client.post(
        "/orchestration-sessions",
        json={
            "conversation_id": conversation["id"],
            "kind": "static_dag",
            "saved_dag_id": saved["id"],
            "draft_dag": {"status": "draft"},
        },
    ).json()["session"]

    response = persistence_client.patch(
        f"/orchestration-sessions/{session['id']}",
        json={
            "saved_dag_id": None,
            "draft_dag": None,
        },
    )

    assert response.status_code == 200
    assert response.json()["session"]["saved_dag_id"] is None
    assert response.json()["session"]["draft_dag"] is None


def test_api_orchestration_session_patch_rejects_null_ui_state(persistence_client) -> None:
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Static DAG", "kind": "static_dag"},
    ).json()["conversation"]
    session = persistence_client.post(
        "/orchestration-sessions",
        json={
            "conversation_id": conversation["id"],
            "kind": "static_dag",
            "ui_state": {"selectedNodeId": "answer"},
        },
    ).json()["session"]

    response = persistence_client.patch(
        f"/orchestration-sessions/{session['id']}",
        json={"ui_state": None},
    )

    assert response.status_code == 422
    stored = state.get_store().get_orchestration_session(session["id"])
    assert stored is not None
    assert json.loads(stored.ui_state_json) == {"selectedNodeId": "answer"}


def test_api_dynamic_dag_stream_updates_standalone_orchestration_session_draft(
    persistence_client,
) -> None:
    state.runner = Runner(
        provider=MockProvider([
            ChatResponse(content=capability_plan_response(
                "tool.echo", {"text": "ok"}, node_id="answer"
            )),
            ChatResponse(content=final_answer_response("Final answer: echo:ok")),
        ])
    )
    project = persistence_client.post(
        "/projects",
        json={"name": "Dynamic", "slug": "dynamic"},
    ).json()["project"]
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Dynamic DAG session", "kind": "dynamic_dag"},
    ).json()["conversation"]
    session = persistence_client.post(
        "/orchestration-sessions",
        json={
            "conversation_id": conversation["id"],
            "kind": "dynamic_dag",
        },
    ).json()["session"]

    response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "echo ok through a DAG",
            "target": "dag",
            "review_level": "fast",
            "conversation_id": conversation["id"],
        },
    )
    updated = persistence_client.get(f"/orchestration-sessions/{session['id']}").json()["session"]
    persisted_runs = state.get_store().list_runs(conversation_id=conversation["id"])
    messages_response = persistence_client.get(f"/conversations/{conversation['id']}/messages")

    assert response.status_code == 200
    assert any(event["type"] == "dag.updated" for event in _sse_events(response.text))
    assert updated["project_id"] is None
    assert updated["draft_dag"]["status"] == "completed"
    assert isinstance(updated["draft_dag"]["version"], int)
    assert updated["draft_dag"]["nodes"]
    assert len(persisted_runs) == 1
    assert persisted_runs[0].project_id is None
    assert persisted_runs[0].workspace_uri == conversation["workspace_uri"]
    assert persisted_runs[0].workspace_uri != project["workspace_uri"]
    assert "/_conversations/" in persisted_runs[0].workspace_uri
    assert messages_response.status_code == 200
    assert messages_response.json()["messages"] == []


def test_api_smart_workbench_dynamic_dag_stream_persists_conversation_messages(
    persistence_client,
) -> None:
    state.runner = Runner(
        provider=MockProvider([
            ChatResponse(content=capability_plan_response(
                "tool.echo", {"text": "ok"}, node_id="answer"
            )),
            ChatResponse(content=final_answer_response("Final answer: echo:ok")),
        ])
    )
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Smart DAG session", "kind": "dynamic_dag"},
    ).json()["conversation"]
    persistence_client.post(
        "/orchestration-sessions",
        json={
            "conversation_id": conversation["id"],
            "kind": "dynamic_dag",
            "ui_state": {"surface": "smart_workbench"},
        },
    ).json()["session"]

    response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "echo ok through a smart DAG",
            "target": "dag",
            "review_level": "fast",
            "conversation_id": conversation["id"],
        },
    )
    result = _sse_events(response.text)[-1]["data"]["result"]
    messages_response = persistence_client.get(f"/conversations/{conversation['id']}/messages")

    assert response.status_code == 200
    assert messages_response.status_code == 200
    messages = messages_response.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "echo ok through a smart DAG"
    assert messages[1]["run_id"] == result["state"]["run_id"]
    assert messages[1]["content"] == "Final answer: echo:ok"
    assert messages[1]["status"] == "completed"
    assert messages[1]["dag"]["status"] == "completed"
    assert any(item["type"] == "dag" for item in messages[1]["timeline"])
    assert any(item["type"] == "text" and item["content"] == "Final answer: echo:ok" for item in messages[1]["timeline"])


def test_api_orchestration_workspace_dynamic_dag_stream_persists_visible_messages(
    persistence_client,
) -> None:
    state.runner = Runner(
        provider=MockProvider([
            ChatResponse(content=capability_plan_response(
                "tool.echo", {"text": "ok"}, node_id="answer"
            )),
            ChatResponse(content=final_answer_response("Final answer: echo:ok")),
        ])
    )
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Dynamic DAG session", "kind": "dynamic_dag"},
    ).json()["conversation"]
    persistence_client.post(
        "/orchestration-sessions",
        json={
            "conversation_id": conversation["id"],
            "kind": "dynamic_dag",
            "ui_state": {"surface": "orchestration_workspace"},
        },
    ).json()["session"]

    response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "visible prompt\n\ninternal DAG context should stay hidden",
            "visible_message": "visible prompt",
            "target": "dag",
            "review_level": "fast",
            "conversation_id": conversation["id"],
        },
    )
    result = _sse_events(response.text)[-1]["data"]["result"]
    messages_response = persistence_client.get(f"/conversations/{conversation['id']}/messages")

    assert response.status_code == 200
    assert result["state"]["status"] == "completed"
    assert messages_response.status_code == 200
    messages = messages_response.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "visible prompt"
    assert messages[0]["run_id"] == result["state"]["run_id"]
    assert messages[1]["run_id"] == result["state"]["run_id"]
    assert messages[1]["status"] == "completed"


def test_api_orchestration_workspace_dynamic_dag_stream_creates_distinct_run_history_entries(
    persistence_client,
) -> None:
    state.runner = Runner(
        provider=MockProvider([
            ChatResponse(content=capability_plan_response(
                "tool.echo", {"text": "one"}, node_id="answer"
            )),
            ChatResponse(content=final_answer_response("first final")),
            ChatResponse(content=capability_plan_response(
                "tool.echo", {"text": "two"}, node_id="answer"
            )),
            ChatResponse(content=final_answer_response("second final")),
        ])
    )
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Dynamic DAG session", "kind": "dynamic_dag"},
    ).json()["conversation"]
    session = persistence_client.post(
        "/orchestration-sessions",
        json={
            "conversation_id": conversation["id"],
            "kind": "dynamic_dag",
            "ui_state": {"surface": "orchestration_workspace"},
        },
    ).json()["session"]

    first_response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "first",
            "target": "dag",
            "review_level": "fast",
            "conversation_id": conversation["id"],
        },
    )
    second_response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "second",
            "target": "dag",
            "review_level": "fast",
            "conversation_id": conversation["id"],
        },
    )
    first_result = _sse_events(first_response.text)[-1]["data"]["result"]
    second_result = _sse_events(second_response.text)[-1]["data"]["result"]
    first_run_id = first_result["state"]["run_id"]
    second_run_id = second_result["state"]["run_id"]
    runs_response = persistence_client.get(f"/orchestration-sessions/{session['id']}/runs")
    messages_response = persistence_client.get(f"/conversations/{conversation['id']}/messages")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_run_id != first_run_id
    assert runs_response.status_code == 200
    run_ids = [run["id"] for run in runs_response.json()["runs"]]
    assert len(run_ids) == 2
    assert set(run_ids) == {first_run_id, second_run_id}
    assert messages_response.status_code == 200
    messages = messages_response.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant"]
    assert [message["run_id"] for message in messages] == [
        first_run_id,
        first_run_id,
        second_run_id,
        second_run_id,
    ]


def test_api_dynamic_dag_stream_keeps_session_when_finished_state_has_no_dag(
    persistence_client,
) -> None:
    class RunnerWithoutDag:
        enable_validation = False

        async def stream(self, *args, **kwargs):
            run_id = "run_without_dag"
            state_without_dag = RunState(run_id=run_id, kind="dynamic_dag", status="completed")
            yield RunStreamEvent(type="run.started", data=RunStartedData(kind="dynamic_dag"), run_id=run_id)
            yield RunStreamEvent(
                type="run.finished",
                data=RunFinishedData(result=RunResult(state=state_without_dag, output_text="done")),
                run_id=run_id,
            )

        def run_state(self, run_id: str):
            return None

        def close(self) -> None:
            pass

    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Dynamic DAG session", "kind": "dynamic_dag"},
    ).json()["conversation"]
    session = persistence_client.post(
        "/orchestration-sessions",
        json={
            "conversation_id": conversation["id"],
            "kind": "dynamic_dag",
            "draft_dag": {
                "dag_id": "existing",
                "task_id": "existing",
                "version": 1,
                "status": "draft",
                "nodes": [],
                "edges": [],
            },
        },
    ).json()["session"]
    state.runner = RunnerWithoutDag()

    response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "finish without a dag",
            "target": "dag",
            "conversation_id": conversation["id"],
        },
    )
    events = _sse_events(response.text)
    updated = persistence_client.get(f"/orchestration-sessions/{session['id']}").json()["session"]

    assert response.status_code == 200
    assert events[-1]["type"] == "run.finished"
    assert updated["draft_dag"]["dag_id"] == "existing"


def test_api_dynamic_dag_review_resume_updates_orchestration_session_draft(
    persistence_client,
) -> None:
    state.runner = Runner(
        provider=MockProvider(
            [
                ChatResponse(
                    content=capability_plan_response(
                        "tool.write_file",
                        {"path": "review.txt", "content": "ok"},
                        node_id="answer",
                    )
                ),
                ChatResponse(
                    content=final_answer_response("Reviewed DAG completed.")
                ),
            ]
        )
    )
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Dynamic DAG session", "kind": "dynamic_dag"},
    ).json()["conversation"]
    session = persistence_client.post(
        "/orchestration-sessions",
        json={
            "conversation_id": conversation["id"],
            "kind": "dynamic_dag",
        },
    ).json()["session"]
    initial_response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "echo ok through a DAG",
            "target": "dag",
            "review_level": "careful",
            "dynamic_adjust": False,
            "conversation_id": conversation["id"],
        },
    )
    initial_result = _sse_events(initial_response.text)[-1]["data"]["result"]
    review_id = initial_result["state"]["pending_review"]["review_id"]
    proposed_dag = initial_result["state"]["pending_review"]["proposed_dag"]

    resume_response = persistence_client.post(
        f"/reviews/{review_id}/resume",
        json={
            "approved": True,
            "review_level": "careful",
            "dag": proposed_dag,
        },
    )
    updated = persistence_client.get(f"/orchestration-sessions/{session['id']}").json()["session"]

    assert resume_response.status_code == 200
    assert updated["draft_dag"]["status"] == "completed"
    assert updated["draft_dag"]["nodes"]


def test_interrupted_dag_review_rejection_clears_pending_review_and_allows_run_delete(
    persistence_client,
) -> None:
    state.runner = Runner(provider=MockProvider([]))
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Interrupted dynamic DAG", "kind": "dynamic_dag"},
    ).json()["conversation"]
    session = persistence_client.post(
        "/orchestration-sessions",
        json={
            "conversation_id": conversation["id"],
            "kind": "dynamic_dag",
        },
    ).json()["session"]
    proposed_dag = _echo_dag(status="review_required")
    run_state = RunState(
        run_id="run_interrupted_dag_rejection",
        kind="dynamic_dag",
        status="awaiting_review",
        dag=DAG(dag_id="dag_echo", task_id="run_interrupted_dag_rejection"),
        pending_review=PendingReview(
            review_id="review_interrupted_dag",
            kind="initial_dag",
            message="Review proposed DAG before execution.",
            proposed_dag=proposed_dag,
        ),
        user_request="echo ok through a DAG",
        review_level="careful",
        runtime_mode="dag",
        dynamic_adjust=True,
    )
    store = state.get_store()
    workspace_path = Path(unquote(urlparse(conversation["workspace_uri"]).path))
    store.create_run(
        run_id=run_state.run_id,
        project_id=None,
        conversation_id=conversation["id"],
        user_id="default",
        kind="dynamic_dag",
        status="awaiting_review",
        workspace_uri=conversation["workspace_uri"],
    )
    store.save_run_state(
        run_state.run_id,
        run_state.model_dump_json(),
        output_text="Review proposed DAG before execution.",
    )
    store.upsert_review(
        review_id="review_interrupted_dag",
        run_id=run_state.run_id,
        project_id=None,
        kind="initial_dag",
    )
    store.append_conversation_message(
        message_id="msg_interrupted_dag_review",
        conversation_id=conversation["id"],
        project_id=None,
        role="assistant",
        run_id=run_state.run_id,
        status="awaiting_review",
        content="Review proposed DAG before execution.",
        pending_review_json=run_state.pending_review.model_dump_json(),
    )
    context = api_app.PersistedMessageContext(
        project_id=None,
        conversation_id=conversation["id"],
        conversation_kind="dynamic_dag",
        workspace_uri=conversation["workspace_uri"],
        workspace_path=workspace_path,
        conversation_state=ConversationState(id=conversation["id"]),
        conversation_revision=0,
        orchestration_session_id=session["id"],
        orchestration_surface=api_app.ORCHESTRATION_WORKSPACE_SURFACE,
    )
    decision = api_app.ReviewDecision(
        review_id="review_interrupted_dag",
        approved=False,
        feedback="Do not run that DAG.",
    )

    async def interrupted_events():
        yield RunStreamEvent(
            type="run.started",
            data=RunStartedData(kind="dynamic_dag"),
            run_id=run_state.run_id,
        )
        raise asyncio.CancelledError

    async def consume_interrupted_resume() -> None:
        projection = await api_app.ConversationMessageProjection.resume_for_review(
            run_state,
            context,
            decision,
        )
        try:
            async for _payload in api_app._persisted_run_events(
                interrupted_events(),
                runner=state.get_runner(),
                context=context,
                stream_id="stream_interrupted_dag_rejection",
                run_kind="dynamic_dag",
                create_run=False,
                existing_run_id=run_state.run_id,
                resolve_review_id=decision.review_id,
                decision_json=api_app._review_decision_json(decision),
                message_projection=projection,
            ):
                pass
        except asyncio.CancelledError:
            pass

    asyncio.run(consume_interrupted_resume())

    stored_run = store.get_run(run_state.run_id)
    stored_state = store.get_run_state(run_state.run_id)
    stored_review = store.get_review("review_interrupted_dag")
    messages = persistence_client.get(f"/conversations/{conversation['id']}/messages").json()["messages"]
    delete_response = persistence_client.delete(f"/runs/{run_state.run_id}")

    assert stored_run is not None
    assert stored_run.status == "failed"
    assert stored_state is not None
    assert stored_state.status == "failed"
    assert stored_state.pending_review is None
    assert stored_review is not None
    assert stored_review.status == "resolved"
    assert len(messages) == 1
    assert messages[0]["status"] == "failed"
    assert messages[0]["pending_review"] is None
    assert delete_response.status_code == 200


def test_interrupted_auto_capability_review_rejection_clears_pending_review_and_allows_run_delete(
    persistence_client,
) -> None:
    state.runner = Runner(provider=MockProvider([]))
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Interrupted auto tool"},
    ).json()["conversation"]
    invocation = CapabilityInvocation(
        invocation_id="call_auto_tool",
        capability_id="tool.read_file",
        kind="tool",
        arguments={"path": "../blocked/secret.txt"},
        risk="medium",
    )
    pending_call = PendingCapabilityCall(
        invocation_id=invocation.invocation_id,
        capability_id=invocation.capability_id,
        tool_name="tool_read_file",
        arguments=invocation.arguments,
    )
    run_state = RunState(
        run_id="run_interrupted_auto_tool_rejection",
        kind="tool",
        status="awaiting_review",
        pending_review=PendingReview(
            review_id="review_interrupted_auto_tool",
            kind="capability_review",
            message="Review capability call: tool.read_file",
            capability_call=pending_call,
        ),
        pending_invocation=invocation,
        user_request="read outside",
        review_level="careful",
        runtime_mode="auto",
    )
    store = state.get_store()
    workspace_path = Path(unquote(urlparse(conversation["workspace_uri"]).path))
    store.create_run(
        run_id=run_state.run_id,
        project_id=None,
        conversation_id=conversation["id"],
        user_id="default",
        kind="tool",
        status="awaiting_review",
        workspace_uri=conversation["workspace_uri"],
    )
    store.save_run_state(
        run_state.run_id,
        run_state.model_dump_json(),
        output_text="Review capability call: tool.read_file",
    )
    store.upsert_review(
        review_id="review_interrupted_auto_tool",
        run_id=run_state.run_id,
        project_id=None,
        kind="capability_review",
    )
    store.append_conversation_message(
        message_id="msg_interrupted_auto_capability_review",
        conversation_id=conversation["id"],
        project_id=None,
        role="assistant",
        run_id=run_state.run_id,
        status="awaiting_review",
        content="Review capability call: tool.read_file",
        timeline_json=json.dumps([
            {
                "type": "capability",
                "status": "awaiting_review",
                "event": {
                    "type": "capability.call.started",
                    "invocation_id": invocation.invocation_id,
                    "capability_id": invocation.capability_id,
                    "arguments": invocation.arguments,
                },
            }
        ]),
        pending_review_json=run_state.pending_review.model_dump_json(),
    )
    context = api_app.PersistedMessageContext(
        project_id=None,
        conversation_id=conversation["id"],
        conversation_kind="chat",
        workspace_uri=conversation["workspace_uri"],
        workspace_path=workspace_path,
        conversation_state=ConversationState(id=conversation["id"]),
        conversation_revision=0,
    )
    decision = api_app.ReviewDecision(
        review_id="review_interrupted_auto_tool",
        approved=False,
        feedback="Do not read that file.",
    )

    async def interrupted_events():
        yield RunStreamEvent(
            type="run.started",
            data=RunStartedData(kind="tool"),
            run_id=run_state.run_id,
        )
        raise asyncio.CancelledError

    async def consume_interrupted_resume() -> None:
        projection = await api_app.ConversationMessageProjection.resume_for_review(
            run_state,
            context,
            decision,
        )
        try:
            async for _payload in api_app._persisted_run_events(
                interrupted_events(),
                runner=state.get_runner(),
                context=context,
                stream_id="stream_interrupted_auto_tool_rejection",
                run_kind="tool",
                create_run=False,
                existing_run_id=run_state.run_id,
                resolve_review_id=decision.review_id,
                decision_json=api_app._review_decision_json(decision),
                message_projection=projection,
            ):
                pass
        except asyncio.CancelledError:
            pass

    asyncio.run(consume_interrupted_resume())

    stored_run = store.get_run(run_state.run_id)
    stored_state = store.get_run_state(run_state.run_id)
    stored_review = store.get_review("review_interrupted_auto_tool")
    messages = persistence_client.get(f"/conversations/{conversation['id']}/messages").json()["messages"]
    capability_items = [
        item for item in messages[0]["timeline"]
        if item["type"] == "capability"
    ]
    delete_response = persistence_client.delete(f"/runs/{run_state.run_id}")

    assert stored_run is not None
    assert stored_run.status == "failed"
    assert stored_state is not None
    assert stored_state.status == "failed"
    assert stored_state.pending_review is None
    assert stored_state.pending_invocation is None
    assert stored_review is not None
    assert stored_review.status == "resolved"
    assert len(messages) == 1
    assert messages[0]["status"] == "failed"
    assert messages[0]["pending_review"] is None
    assert capability_items[0]["status"] == "rejected"
    assert capability_items[0]["result"]["type"] == "capability.call.failed"
    assert delete_response.status_code == 200


def test_auto_capability_review_rejection_is_persisted_before_resume_stream_body_starts(
    persistence_client,
) -> None:
    state.runner = Runner(
        provider=MockProvider(
            [
                ChatResponse(
                    tool_calls=[
                        ToolCall(
                            id="call_auto_tool_early",
                            name="tool_read_file",
                            arguments={"path": "../blocked/secret.txt"},
                        )
                    ]
                )
            ]
        )
    )
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Early aborted auto tool"},
    ).json()["conversation"]
    initial_response = persistence_client.post(
        "/messages/stream",
        json={
            "input": "read outside",
            "target": "tool",
            "capability_ids": ["tool.read_file"],
            "conversation_id": conversation["id"],
        },
    )
    initial_result = _sse_events(initial_response.text)[-1]["data"]["result"]
    run_id = initial_result["state"]["run_id"]
    review_id = initial_result["state"]["pending_review"]["review_id"]
    store = state.get_store()

    async def open_resume_without_reading_body() -> None:
        response = await api_app._resume_persisted_review_stream(
            review_id,
            api_app.ProjectResumeReviewRequest(
                approved=False,
                feedback="Do not read that file.",
            ),
            project_id=None,
        )
        close = getattr(response.body_iterator, "aclose", None)
        if callable(close):
            await close()

    asyncio.run(open_resume_without_reading_body())

    stored_run = store.get_run(run_id)
    stored_state = store.get_run_state(run_id)
    stored_review = store.get_review(review_id)
    stored_checkpoint = store.get_run_checkpoint(run_id)
    messages = persistence_client.get(f"/conversations/{conversation['id']}/messages").json()["messages"]
    events = persistence_client.get(f"/runs/{run_id}/events").json()["events"]
    failed_events = [
        event for event in events
        if event["event_type"] == "run.failed"
    ]
    lock = store.acquire_conversation_lock(conversation["id"], owner="after_early_close")
    lock.release()

    assert stored_run is not None
    assert stored_run.status == "failed"
    assert stored_state is not None
    assert stored_state.status == "failed"
    assert stored_state.pending_review is None
    assert stored_state.pending_invocation is None
    assert stored_review is not None
    assert stored_review.status == "resolved"
    assert stored_checkpoint is None
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["pending_review"] is None
    assert messages[-1]["status"] == "failed"
    assert failed_events
    assert failed_events[-1]["payload"]["data"]["error_type"] == "ClientDisconnect"


def _sse_events(text: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for block in text.strip().split("\n\n"):
        if not block:
            continue
        line = block.strip()
        assert line.startswith("data: ")
        events.append(json.loads(line.removeprefix("data: ")))
    return events
