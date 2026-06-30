import json
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse, urlsplit

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
    run = store.create_run(
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

    escaped_list = persistence_client.get(f"/projects/{project_id}/files", params={"path": "../outside"})
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
    response = persistence_client.post("/conversations", json={"title": "Inbox chat"})
    listed = persistence_client.get("/conversations")

    assert response.status_code == 200
    conversation = response.json()["conversation"]
    workspace_path = Path(unquote(urlparse(conversation["workspace_uri"]).path))
    assert conversation["id"].startswith("conv_")
    assert conversation["project_id"] is None
    assert conversation["title"] == "Inbox chat"
    assert workspace_path.is_dir()
    assert workspace_path.name == "workspace"
    assert workspace_path.parent.name == conversation["id"]
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["conversations"]] == [conversation["id"]]


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
            "messages": [{"role": "user", "content": "write project note"}],
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
            "messages": [{"role": "user", "content": "write inbox note"}],
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


def test_api_deletes_standalone_conversation_workspace(persistence_client) -> None:
    conversation = persistence_client.post(
        "/conversations",
        json={"title": "Inbox chat"},
    ).json()["conversation"]
    conversation_workspace = Path(unquote(urlparse(conversation["workspace_uri"]).path))
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
            "messages": [{"role": "user", "content": "hi"}],
            "target": "tool",
            "conversation_id": conversation["id"],
        },
    )

    assert response.status_code == 400
    assert "project_id" in response.json()["detail"]


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


def test_api_project_message_stream_continues_conversation_from_db_state(persistence_client) -> None:
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
            "messages": [{"role": "user", "content": "first"}],
            "target": "tool",
            "project_id": project["id"],
            "conversation_id": conversation["id"],
        },
    )
    second_response = persistence_client.post(
        "/messages/stream",
        json={
            "messages": [{"role": "user", "content": "second"}],
            "target": "tool",
            "project_id": project["id"],
            "conversation_id": conversation["id"],
        },
    )
    first_result = _sse_events(first_response.text)[-1]["data"]["result"]
    second_result = _sse_events(second_response.text)[-1]["data"]["result"]

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_result["state"]["run_id"] == first_result["state"]["run_id"]
    assert second_result["output_text"] == "second done"
    assert state.get_store().get_run(first_result["state"]["run_id"]).output_text == "second done"


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
            "messages": [{"role": "user", "content": "read outside"}],
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


def _sse_events(text: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for block in text.strip().split("\n\n"):
        if not block:
            continue
        line = block.strip()
        assert line.startswith("data: ")
        events.append(json.loads(line.removeprefix("data: ")))
    return events
