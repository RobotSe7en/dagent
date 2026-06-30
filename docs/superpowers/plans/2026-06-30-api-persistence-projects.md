# API Persistence And Project Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add API-managed project, conversation, run, review, and event persistence while keeping the public SDK free of database concerns and making project sessions run in the project workspace.

**Architecture:** The SDK gets one narrow workspace execution extension: callers may pass an exact `workspace_path` so a run can use a project directory without creating a run subdirectory. The API owns persistence, project/conversation state, SQLite storage, workspace URI resolution, and conversation single-writer locking. Projects share one physical workspace and are not locked; only the same conversation/session is locked.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, SQLite with WAL, stdlib `sqlite3`, `fastapi.concurrency.run_in_threadpool`, React/Vite WebUI, pytest, npm tests.

---

## Final Decisions

- SDK persistence: none. `dagent.Runner` and declarative agent objects do not know about projects, users, databases, or workspace URIs.
- SDK workspace extension: add optional `workspace_path` to `Runner.run(...)` and `Runner.stream(...)`.
- Project workspace: each project owns one shared workspace directory, for example `.dagent/projects/<project_id>/workspace`.
- Project locking: no project lock. Multiple conversations in one project may run concurrently and may touch the same files.
- Conversation locking: same conversation/session has a single writer. A second stream/resume for the same conversation returns `409`.
- Hosted project mode: API does not trust client-supplied `state` or `workspace_root`; it loads state from DB and supplies `workspace_path`.
- Event durability: persist events with a DB event id. SDK `RunStreamEvent.sequence` is stream-local and is not a durable primary key.
- Artifacts: no `run_artifacts` table in v1. Artifacts are derived from `RunState.trace.artifacts` and the project workspace file tree.
- Messages: no `conversation_messages` table in v1. Conversation history is derived from `RunState.internal_messages`.
- Enterprise path: Postgres store, worker run leases, object storage workspace store, optional worktree or sandbox isolation.

## File Map

### SDK

- Modify `dagent/runner.py`
  - Add `workspace_path: str | Path | None = None` to `Runner.run(...)`, `Runner.stream(...)`, and `_run_dispatch(...)`.
  - Pass `workspace_path` into runtime message and static DAG execution.
  - Validate explicit `workspace_path` against `state.workspace_path` when continuing state.
- Modify `dagent/harness_runtime/runtime.py`
  - Add `workspace_path` to `handle_messages(...)` and `run_dag_spec(...)`.
  - Resolve exact workspace paths without creating run subdirectories.
  - Keep current `<workspace_root>/<run_id>` behavior when no exact path is supplied.
- Modify `dagent/harness_runtime/dag_agent.py`
  - Add `workspace_path` to `RuntimeDAGAgent.run_static(...)`.
  - Use exact workspace path for static DAG execution when supplied.
- Modify SDK docs:
  - `docs/en/runner-and-configuration.md`
  - `docs/zh-CN/runner-and-configuration.md`

### API Backend

- Create `api/storage/__init__.py`
- Create `api/storage/base.py`
- Create `api/storage/models.py`
- Create `api/storage/schema.sql`
- Create `api/storage/sqlite.py`
- Create `api/workspaces.py`
- Modify `api/app.py`
  - Add storage/workspace state.
  - Add project/conversation/run/event endpoints.
  - Extend `/messages/stream` and `/messages/resume` with project context.
  - Persist stream events, run snapshots, review rows, and conversation locks.
  - Make trace/artifact lookup DB-first.

### Web UI

- Modify `web/src/types.ts`
  - Add `Project`, `Conversation`, `RunSummary`, `RunStreamSummary` types.
- Modify `web/src/api.ts`
  - Add project/conversation API calls.
  - Add project context to stream/resume requests.
- Modify `web/src/App.tsx`
  - Add project and conversation selection state.
  - Send project context instead of client state in project mode.
  - Restore conversation state from backend.
- Modify `web/src/styles.css`
  - Add restrained project/conversation navigation styles.

### Tests

- Modify `tests/test_workspace_defaults.py`
- Modify `tests/test_agent_sdk_public_api.py`
- Create `tests/test_api_persistence.py`
- Extend `tests/test_api.py` where existing endpoints need DB-first fallback coverage.
- Run existing WebUI tests and build.

## Data Model

Use `INTEGER` unix seconds for timestamps in SQLite v1. Use `TEXT` JSON columns in SQLite; map them to `jsonb` in Postgres later.

```sql
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

CREATE TABLE projects (
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
    archived_at INTEGER,
    UNIQUE(org_id, slug)
);

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    org_id TEXT NOT NULL DEFAULT 'default',
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    last_run_id TEXT,
    active_run_id TEXT,
    active_stream_id TEXT,
    active_kind TEXT,
    active_expires_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    archived_at INTEGER
);

CREATE INDEX idx_conversations_project_updated
    ON conversations(project_id, updated_at DESC);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
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
    updated_at INTEGER NOT NULL
);

CREATE INDEX idx_runs_project_updated ON runs(project_id, updated_at DESC);
CREATE INDEX idx_runs_conversation_updated ON runs(conversation_id, updated_at DESC);
CREATE INDEX idx_runs_worker_lease ON runs(status, lease_expires_at);

CREATE TABLE run_streams (
    id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    input_json TEXT NOT NULL,
    output_text TEXT NOT NULL DEFAULT '',
    error_json TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER,
    updated_at INTEGER NOT NULL
);

CREATE INDEX idx_run_streams_conversation_updated
    ON run_streams(conversation_id, updated_at DESC);

CREATE TABLE run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id TEXT NOT NULL REFERENCES run_streams(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    sdk_sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(stream_id, sdk_sequence)
);

CREATE INDEX idx_run_events_stream_id_id ON run_events(stream_id, id);
CREATE INDEX idx_run_events_run_id_id ON run_events(run_id, id);

CREATE TABLE reviews (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    stream_id TEXT REFERENCES run_streams(id) ON DELETE SET NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    org_id TEXT NOT NULL DEFAULT 'default',
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    decision_json TEXT,
    created_at INTEGER NOT NULL,
    resolved_at INTEGER
);

CREATE INDEX idx_reviews_conversation_pending
    ON reviews(conversation_id, status, created_at DESC);
```

## Task 1: SDK Exact Workspace Path

**Files:**
- Modify `dagent/runner.py`
- Modify `dagent/harness_runtime/runtime.py`
- Modify `dagent/harness_runtime/dag_agent.py`
- Test `tests/test_workspace_defaults.py`
- Test `tests/test_agent_sdk_public_api.py`

- [ ] **Step 1: Add failing SDK tests for exact workspace path**

Add tests that assert a `workspace_path` is used directly and no `<run_id>` subdirectory is created:

```python
async def test_runner_stream_uses_exact_workspace_path(tmp_path):
    workspace = tmp_path / "project-workspace"
    provider = MockProvider([ChatResponse(content="done")])
    runner = Runner(provider=provider)
    agent = ToolAgent(name="assistant")

    result = await runner.run(
        agent,
        messages=[{"role": "user", "content": "finish"}],
        workspace_path=workspace,
    )

    assert Path(result.state.workspace_path) == workspace.resolve()
    assert workspace.exists()
    assert not (workspace / result.state.run_id).exists()
```

Add a continuation conflict test:

```python
async def test_runner_rejects_conflicting_exact_workspace_path(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    provider = MockProvider([ChatResponse(content="one"), ChatResponse(content="two")])
    runner = Runner(provider=provider)
    agent = ToolAgent(name="assistant")
    result = await runner.run(
        agent,
        messages=[{"role": "user", "content": "one"}],
        workspace_path=first,
    )

    with pytest.raises(ValueError, match="workspace_path"):
        await runner.run(
            agent,
            messages=[{"role": "user", "content": "two"}],
            state=result.state,
            workspace_path=second,
        )
```

- [ ] **Step 2: Run failing SDK workspace tests**

Run:

```bash
uv run --extra dev pytest tests/test_workspace_defaults.py tests/test_agent_sdk_public_api.py -q
```

Expected: tests that reference `workspace_path` fail because the parameter is not implemented.

- [ ] **Step 3: Implement `workspace_path` on Runner**

Update public signatures:

```python
async def run(
    self,
    target: RunTarget,
    *,
    messages: list[dict[str, Any]] | None = None,
    state: RunState | None = None,
    graph_input: Any = None,
    review: ReviewLevel | None = None,
    dynamic_adjust: bool | None = None,
    execution: RunExecution = "local",
    workspace_root: str | Path = DEFAULT_RUNS_DIR,
    workspace_path: str | Path | None = None,
    input_uploads: list[ArtifactUpload] | None = None,
    artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
    on_token: TokenHandler | None = None,
    on_event: LoopEventHandler | None = None,
) -> RunResult:
```

Update `Runner.stream(...)` with the same `workspace_path` keyword and pass it through to `self.run(...)`.

Add a helper in `runner.py`:

```python
def _validated_workspace_path_for_state(
    state: RunState | None,
    workspace_path: str | Path | None,
) -> Path | None:
    if workspace_path is None:
        return None
    resolved = Path(workspace_path).expanduser().resolve()
    if state is not None and state.workspace_path:
        state_path = Path(state.workspace_path).expanduser().resolve()
        if state_path != resolved:
            raise ValueError(
                f"workspace_path '{resolved}' does not match resume state workspace_path '{state_path}'."
            )
    return resolved
```

Use the helper before dispatch and pass the resolved path into `_run_dispatch(...)`.

- [ ] **Step 4: Implement runtime exact workspace resolution**

In `dagent/harness_runtime/runtime.py`, update `handle_messages(...)`:

```python
async def handle_messages(
    self,
    messages: list[dict[str, Any]],
    *,
    run_state: RunState | None = None,
    mode: RuntimeMode = "auto",
    review_level: ReviewLevel = "fast",
    dynamic_adjust: bool = True,
    workspace_root: str | Path = DEFAULT_RUNS_DIR,
    workspace_path: str | Path | None = None,
    input_uploads: list[ArtifactUpload] | None = None,
    capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
    on_token: TokenHandler | None = None,
    on_event: LoopEventHandler | None = None,
) -> RunResult:
```

Replace workspace selection with:

```python
workspace = self._workspace_path_for_run(
    run_state,
    workspace_root,
    run_id,
    workspace_path=workspace_path,
)
```

Update `_workspace_path_for_run(...)`:

```python
def _workspace_path_for_run(
    self,
    run_state: RunState | None,
    workspace_root: str | Path,
    run_id: str,
    *,
    workspace_path: str | Path | None = None,
) -> Path:
    if run_state is not None and run_state.workspace_path:
        path = Path(run_state.workspace_path).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    if workspace_path is not None:
        path = Path(workspace_path).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return create_run_workspace(self._resolve_run_workspace_root(workspace_root), run_id=run_id)
```

- [ ] **Step 5: Implement static DAG exact workspace path**

Add `workspace_path` to `HarnessRuntime.run_dag_spec(...)`, pass it to `_execute_loop(...)`, and add it to `RuntimeDAGAgent.run_static(...)`.

In `RuntimeDAGAgent.run_static(...)`, replace unconditional `create_run_workspace(...)` with:

```python
if workspace_path is not None:
    workspace = Path(workspace_path).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
else:
    workspace = create_run_workspace(
        resolve_run_workspace_root(workspace_parent_root, workspace_root),
        run_id=run_id,
    )
```

- [ ] **Step 6: Verify SDK tests pass**

Run:

```bash
uv run --extra dev pytest tests/test_workspace_defaults.py tests/test_agent_sdk_public_api.py -q
```

Expected: PASS.

- [ ] **Step 7: Update SDK docs**

Document:

- `workspace_root` keeps the existing run-scoped behavior.
- `workspace_path` opts into an exact execution directory.
- `workspace_path` is not persistence; it is a runtime execution path.
- State continuation reuses `RunState.workspace_path`.

- [ ] **Step 8: Commit SDK workspace work**

```bash
git add dagent/runner.py dagent/harness_runtime/runtime.py dagent/harness_runtime/dag_agent.py \
  tests/test_workspace_defaults.py tests/test_agent_sdk_public_api.py \
  docs/en/runner-and-configuration.md docs/zh-CN/runner-and-configuration.md
git commit -m "feat: support exact run workspace paths"
```

## Task 2: API Storage And Workspace Abstractions

**Files:**
- Create `api/storage/__init__.py`
- Create `api/storage/base.py`
- Create `api/storage/models.py`
- Create `api/storage/schema.sql`
- Create `api/storage/sqlite.py`
- Create `api/workspaces.py`
- Test `tests/test_api_persistence.py`

- [ ] **Step 1: Add storage model tests**

Create `tests/test_api_persistence.py` with tests for project/conversation creation, run state save/load, and event id ordering.

```python
def test_sqlite_store_persists_run_state(tmp_path):
    store = SQLiteStore(tmp_path / "state.db")
    project = store.create_project(slug="demo", name="Demo")
    conversation = store.create_conversation(project.id, title="Main")
    state = RunState(run_id="tool_run_1", kind="tool", status="completed", workspace_path="/tmp/ws")

    store.upsert_run(
        run_id=state.run_id,
        project_id=project.id,
        conversation_id=conversation.id,
        kind=state.kind,
        status=state.status,
        workspace_uri=project.workspace_uri,
        state_json=state.model_dump_json(),
        output_text="done",
    )

    loaded = store.get_run_state(state.run_id)
    assert loaded == state
```

```python
def test_run_events_use_database_event_ids(tmp_path):
    store = SQLiteStore(tmp_path / "state.db")
    project = store.create_project(slug="demo", name="Demo")
    conversation = store.create_conversation(project.id, title="Main")
    stream = store.create_run_stream(project.id, conversation.id, input_json="{}")

    first = store.append_run_event(stream.id, run_id=None, sdk_sequence=1, event_type="token", payload_json="{}")
    second = store.append_run_event(stream.id, run_id=None, sdk_sequence=2, event_type="token", payload_json="{}")

    assert first.id < second.id
    assert [event.id for event in store.list_run_events(stream.id, after_id=first.id)] == [second.id]
```

- [ ] **Step 2: Run storage tests and confirm failure**

Run:

```bash
uv run --extra dev pytest tests/test_api_persistence.py -q
```

Expected: import failures for missing storage modules.

- [ ] **Step 3: Create storage models**

In `api/storage/models.py`, define Pydantic models:

```python
class ProjectRecord(BaseModel):
    id: str
    org_id: str = "default"
    owner_user_id: str = "default"
    slug: str
    name: str
    description: str | None = None
    workspace_uri: str
    settings_json: str = "{}"
    created_at: int
    updated_at: int
    archived_at: int | None = None
```

Also define `ConversationRecord`, `RunRecord`, `RunStreamRecord`, `RunEventRecord`, and `ReviewRecord` with fields matching the schema.

- [ ] **Step 4: Create Store protocol**

In `api/storage/base.py`, define a synchronous `Store` protocol with these methods:

```python
class Store(Protocol):
    def create_project(self, *, slug: str, name: str, description: str | None = None) -> ProjectRecord: ...
    def list_projects(self, *, org_id: str = "default") -> list[ProjectRecord]: ...
    def get_project(self, project_id: str) -> ProjectRecord | None: ...
    def create_conversation(self, project_id: str, *, title: str) -> ConversationRecord: ...
    def list_conversations(self, project_id: str) -> list[ConversationRecord]: ...
    def get_conversation(self, conversation_id: str) -> ConversationRecord | None: ...
    def acquire_conversation_lock(self, conversation_id: str, *, stream_id: str, kind: str, run_id: str | None, lease_seconds: int) -> ConversationRecord: ...
    def renew_conversation_lock(self, conversation_id: str, *, stream_id: str, lease_seconds: int) -> bool: ...
    def mark_conversation_awaiting_review(self, conversation_id: str, *, stream_id: str, run_id: str) -> None: ...
    def release_conversation_lock(self, conversation_id: str, *, stream_id: str) -> None: ...
    def clear_expired_running_locks(self) -> int: ...
    def create_run_stream(self, project_id: str, conversation_id: str, *, input_json: str) -> RunStreamRecord: ...
    def update_run_stream(self, stream_id: str, **fields: Any) -> None: ...
    def upsert_run(self, *, run_id: str, project_id: str, conversation_id: str, kind: str | None, status: str, workspace_uri: str, state_json: str | None, output_text: str = "", error_json: str | None = None) -> RunRecord: ...
    def get_run(self, run_id: str) -> RunRecord | None: ...
    def get_run_state(self, run_id: str) -> RunState | None: ...
    def append_run_event(self, stream_id: str, *, run_id: str | None, sdk_sequence: int, event_type: str, payload_json: str) -> RunEventRecord: ...
    def list_run_events(self, stream_id: str, *, after_id: int = 0) -> list[RunEventRecord]: ...
    def upsert_review(self, *, review_id: str, run_id: str, stream_id: str | None, project_id: str, conversation_id: str, kind: str, status: str) -> ReviewRecord: ...
    def get_review(self, review_id: str) -> ReviewRecord | None: ...
    def resolve_review(self, review_id: str, *, decision_json: str) -> None: ...
```

- [ ] **Step 5: Implement SQLiteStore**

In `api/storage/sqlite.py`:

- Open one connection with `check_same_thread=False`.
- Protect every connection use with `threading.Lock`.
- Set `row_factory = sqlite3.Row`.
- Run these pragmas:

```python
PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
)
```

Load and execute `schema.sql` on initialization. Use `uuid.uuid4().hex` for generated ids with prefixes:

- `proj_`
- `conv_`
- `stream_`

Raise `ConversationBusyError` from `api/storage/base.py` when acquiring a busy conversation.

- [ ] **Step 6: Implement local workspace store**

In `api/workspaces.py`:

```python
class WorkspaceStore(Protocol):
    def project_workspace_uri(self, project_id: str) -> str: ...
    def local_path_for(self, uri: str) -> Path: ...
    def open_file(self, uri: str, rel_path: str) -> BinaryIO: ...
```

Implement `LocalWorkspaceStore`:

```python
class LocalWorkspaceStore:
    def __init__(self, root: str | Path = DEFAULT_WORKSPACE):
        self.root = Path(root).expanduser().resolve()

    def project_workspace_uri(self, project_id: str) -> str:
        validate_capability_id_segment(project_id.removeprefix("proj_") or project_id)
        path = self.root / "projects" / project_id / "workspace"
        path.mkdir(parents=True, exist_ok=True)
        return path.as_uri()

    def local_path_for(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise ValueError(f"Unsupported local workspace URI scheme: {parsed.scheme}")
        path = Path(unquote(parsed.path)).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
```

Ensure `open_file(...)` rejects path traversal by resolving the relative path under the workspace root.

- [ ] **Step 7: Verify storage tests pass**

Run:

```bash
uv run --extra dev pytest tests/test_api_persistence.py -q
```

Expected: PASS for storage-level tests.

- [ ] **Step 8: Commit storage layer**

```bash
git add api/storage api/workspaces.py tests/test_api_persistence.py
git commit -m "feat: add API persistence storage"
```

## Task 3: Project And Conversation API

**Files:**
- Modify `api/app.py`
- Extend `tests/test_api_persistence.py`

- [ ] **Step 1: Add API tests for project and conversation CRUD**

Add tests:

```python
def test_project_and_conversation_crud(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "state.db")
    monkeypatch.setattr(app_module.state, "store", store)
    monkeypatch.setattr(app_module.state, "workspaces", LocalWorkspaceStore(tmp_path / ".dagent"))
    client = TestClient(app)

    project_response = client.post("/projects", json={"name": "Demo", "slug": "demo"})
    assert project_response.status_code == 200
    project = project_response.json()
    assert project["name"] == "Demo"

    conversation_response = client.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "Main"},
    )
    assert conversation_response.status_code == 200
    conversation = conversation_response.json()
    assert conversation["project_id"] == project["id"]

    listed = client.get(f"/projects/{project['id']}/conversations")
    assert listed.status_code == 200
    assert listed.json()["conversations"][0]["id"] == conversation["id"]
```

- [ ] **Step 2: Add request/response models in api/app.py**

Add:

```python
class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    slug: str | None = None
    description: str | None = None


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
```

Add response helpers that return plain JSON dictionaries with `model_dump(mode="json")`.

- [ ] **Step 3: Add store/workspace state to ApiState**

In `ApiState.__init__`:

```python
self.store: Store = SQLiteStore(Path(DEFAULT_WORKSPACE) / "api-state.db")
self.workspaces: WorkspaceStore = LocalWorkspaceStore(DEFAULT_WORKSPACE)
```

Keep tests able to monkeypatch these attributes.

- [ ] **Step 4: Add CRUD routes**

Add:

```python
@app.post("/projects")
async def create_project(request: CreateProjectRequest) -> dict[str, Any]:
    slug = request.slug or _slug_from_project_name(request.name)
    project = await run_in_threadpool(
        state.store.create_project,
        slug=slug,
        name=request.name,
        description=request.description,
    )
    return project.model_dump(mode="json")
```

Add `GET /projects`, `GET /projects/{project_id}`, `POST /projects/{project_id}/conversations`, `GET /projects/{project_id}/conversations`, and `GET /projects/{project_id}/conversations/{conversation_id}`.

- [ ] **Step 5: Verify CRUD tests pass**

Run:

```bash
uv run --extra dev pytest tests/test_api_persistence.py::test_project_and_conversation_crud -q
```

Expected: PASS.

- [ ] **Step 6: Commit CRUD API**

```bash
git add api/app.py tests/test_api_persistence.py
git commit -m "feat: add project conversation APIs"
```

## Task 4: Persistent Project Message Stream

**Files:**
- Modify `api/app.py`
- Extend `tests/test_api_persistence.py`

- [ ] **Step 1: Add project stream validation tests**

Add tests that project mode rejects client `state` and `workspace_root`:

```python
def test_project_message_stream_rejects_client_state(tmp_path, monkeypatch):
    client, project, conversation = configured_persistence_client(tmp_path, monkeypatch)
    state = RunState(run_id="tool_run_1", kind="tool", status="completed")

    response = client.post("/messages/stream", json={
        "project_id": project["id"],
        "conversation_id": conversation["id"],
        "messages": [{"role": "user", "content": "hello"}],
        "state": state.model_dump(mode="json"),
    })

    assert response.status_code == 400
    assert "state" in response.json()["detail"]
```

- [ ] **Step 2: Extend MessageRequest**

In `MessageRequest`, add:

```python
project_id: str | None = None
conversation_id: str | None = None
```

Add helper:

```python
def _project_context_from_message(request: MessageRequest) -> ProjectContext | None:
    if request.project_id is None and request.conversation_id is None:
        return None
    if not request.project_id or not request.conversation_id:
        raise HTTPException(status_code=400, detail="project_id and conversation_id must be provided together.")
    if request.state is not None:
        raise HTTPException(status_code=400, detail="Project message streams do not accept client state.")
    if request.workspace_root is not None:
        raise HTTPException(status_code=400, detail="Project message streams do not accept workspace_root.")
    return ProjectContext(project_id=request.project_id, conversation_id=request.conversation_id)
```

- [ ] **Step 3: Implement project stream flow**

In `/messages/stream`, branch once:

```python
project_context = _project_context_from_message(request)
if project_context is None:
    return _local_message_stream(request, input_uploads)
return _project_message_stream(request, input_uploads, project_context)
```

Keep the existing local behavior inside `_local_message_stream(...)`.

In `_project_message_stream(...)`:

- Create `stream_id`.
- Acquire conversation lock.
- Load `conversation.last_run_id` state from DB.
- Resolve `workspace_path = state.workspaces.local_path_for(project.workspace_uri)`.
- Call `runner.stream(..., state=stored_state, workspace_path=workspace_path)`.
- Persist every event with `append_run_event(...)` through `run_in_threadpool`.
- On `run.started`, upsert run and attach `run_id` to the stream.
- On `run.finished`, save `RunState.model_dump_json()` and output.
- If final state has `pending_review`, upsert review and mark conversation awaiting review.
- Otherwise release conversation lock.

- [ ] **Step 4: Add stream persistence test**

Add a test using `MockProvider`:

```python
def test_project_message_stream_persists_state_and_uses_project_workspace(tmp_path, monkeypatch):
    client, project, conversation = configured_persistence_client(tmp_path, monkeypatch)
    app_module.state.runner = Runner(provider=MockProvider([ChatResponse(content="done")]))

    with client.stream("POST", "/messages/stream", json={
        "project_id": project["id"],
        "conversation_id": conversation["id"],
        "messages": [{"role": "user", "content": "hello"}],
        "target": "tool",
    }) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert "run.finished" in body
    stored = app_module.state.store.get_conversation(conversation["id"])
    assert stored.last_run_id
    run_state = app_module.state.store.get_run_state(stored.last_run_id)
    assert run_state is not None
    assert Path(run_state.workspace_path) == Path(urlparse(project["workspace_uri"]).path).resolve()
```

- [ ] **Step 5: Verify project stream tests**

Run:

```bash
uv run --extra dev pytest tests/test_api_persistence.py -q
```

Expected: project stream tests pass.

- [ ] **Step 6: Commit project stream persistence**

```bash
git add api/app.py tests/test_api_persistence.py
git commit -m "feat: persist project message streams"
```

## Task 5: Conversation Locking And Review Resume

**Files:**
- Modify `api/app.py`
- Extend `tests/test_api_persistence.py`

- [ ] **Step 1: Add conversation lock tests**

Add tests:

```python
def test_same_conversation_running_lock_returns_409(tmp_path, monkeypatch):
    client, project, conversation = configured_persistence_client(tmp_path, monkeypatch)
    stream_id = "stream_busy"
    app_module.state.store.acquire_conversation_lock(
        conversation["id"],
        stream_id=stream_id,
        kind="running",
        run_id=None,
        lease_seconds=60,
    )

    response = client.post("/messages/stream", json={
        "project_id": project["id"],
        "conversation_id": conversation["id"],
        "messages": [{"role": "user", "content": "hello"}],
    })

    assert response.status_code == 409
```

Add a test that another conversation in the same project is not blocked.

- [ ] **Step 2: Implement lock error mapping**

Catch `ConversationBusyError` in project stream and return:

```python
raise HTTPException(status_code=409, detail=str(exc))
```

Renew running locks every 20 seconds during stream. Use an async helper:

```python
async def _renew_conversation_lock_until_done(conversation_id: str, stream_id: str, done: asyncio.Event) -> None:
    while not done.is_set():
        await asyncio.sleep(20)
        await run_in_threadpool(
            state.store.renew_conversation_lock,
            conversation_id,
            stream_id=stream_id,
            lease_seconds=60,
        )
```

- [ ] **Step 3: Extend ResumeReviewRequest**

Add:

```python
project_id: str | None = None
conversation_id: str | None = None
```

Project resume mode rejects client `state` and requires both project and conversation ids.

- [ ] **Step 4: Implement DB-backed resume**

In `/messages/resume`, branch:

- No project context: keep current local behavior.
- Project context: load review from DB, verify project/conversation, load `RunState` from `runs.state_json`, and call `runner.resume_stream(decision, state=run_state)`.

On final state:

- Save `runs.state_json`.
- Resolve review with `decision_json`.
- If there is another `pending_review`, keep conversation awaiting review.
- Otherwise release conversation lock.

- [ ] **Step 5: Add restart resume test**

Simulate restart with a new runner and same DB:

```python
def test_project_review_resume_uses_db_state_after_restart(tmp_path, monkeypatch):
    client, project, conversation = configured_persistence_client(tmp_path, monkeypatch)
    pending = PendingReview(review_id="review_1", kind="capability_review", capability_call=..., payload={})
    state = RunState(
        run_id="tool_run_1",
        kind="tool",
        status="awaiting_review",
        pending_review=pending,
        workspace_path=str(Path(urlparse(project["workspace_uri"]).path)),
    )
    app_module.state.store.upsert_run(
        run_id=state.run_id,
        project_id=project["id"],
        conversation_id=conversation["id"],
        kind=state.kind,
        status=state.status,
        workspace_uri=project["workspace_uri"],
        state_json=state.model_dump_json(),
    )
    app_module.state.store.upsert_review(
        review_id="review_1",
        run_id=state.run_id,
        stream_id=None,
        project_id=project["id"],
        conversation_id=conversation["id"],
        kind="capability_review",
        status="pending",
    )

    app_module.state.close_runner()
    app_module.state.runner = Runner(provider=MockProvider([ChatResponse(content="resumed")]))

    response = client.post("/messages/resume", json={
        "project_id": project["id"],
        "conversation_id": conversation["id"],
        "review_id": "review_1",
        "approved": True,
    })

    assert response.status_code == 200
```

Fill the `capability_call` with the existing test fixture shape from `tests/test_api.py` so the SDK accepts the state.

- [ ] **Step 6: Verify resume and lock tests**

Run:

```bash
uv run --extra dev pytest tests/test_api_persistence.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit locking and resume**

```bash
git add api/app.py tests/test_api_persistence.py
git commit -m "feat: persist project review resume"
```

## Task 6: DB-First Trace And Artifact Lookup

**Files:**
- Modify `api/app.py`
- Extend `tests/test_api.py`
- Extend `tests/test_api_persistence.py`

- [ ] **Step 1: Add DB-first trace/artifact tests**

Add a test that creates a run state only in DB, clears `state.runner`, and calls:

- `GET /runs/{run_id}/trace`
- `GET /runs/{run_id}/artifacts`

Expected: both use DB state.

- [ ] **Step 2: Update `_run_state_from_state`**

Replace current memory-only lookup with:

```python
def _run_state_from_state(run_id: str) -> RunState | None:
    try:
        stored = state.store.get_run_state(run_id)
    except Exception:
        stored = None
    if stored is not None:
        return stored
    if state.runner is None:
        return None
    return state.runner.run_state(run_id)
```

- [ ] **Step 3: Update `/runs/{run_id}/trace`**

Use `_run_state_from_state(run_id)` first:

```python
@app.get("/runs/{run_id}/trace")
async def get_run_trace(run_id: str) -> dict[str, Any]:
    run_state = _run_state_from_state(run_id)
    if run_state is not None and run_state.trace is not None:
        return {"run_id": run_id, "trace": run_state.trace.model_dump(mode="json")}
    if state.runner is not None:
        trace = state.runner.run_trace(run_id)
        if trace is not None:
            return {"run_id": run_id, "trace": trace.model_dump(mode="json")}
    raise HTTPException(status_code=404, detail="Run not found.")
```

- [ ] **Step 4: Verify DB-first tests**

Run:

```bash
uv run --extra dev pytest tests/test_api_persistence.py tests/test_api.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit DB-first lookup**

```bash
git add api/app.py tests/test_api.py tests/test_api_persistence.py
git commit -m "feat: serve persisted run state"
```

## Task 7: Web Project And Conversation UI

**Files:**
- Modify `web/src/types.ts`
- Modify `web/src/api.ts`
- Modify `web/src/App.tsx`
- Modify `web/src/styles.css`

- [ ] **Step 1: Add TypeScript API types**

In `web/src/types.ts`, add:

```ts
export interface Project {
  id: string;
  slug: string;
  name: string;
  description?: string | null;
  workspace_uri: string;
  created_at: number;
  updated_at: number;
}

export interface Conversation {
  id: string;
  project_id: string;
  title: string;
  status: string;
  last_run_id?: string | null;
  active_kind?: string | null;
  created_at: number;
  updated_at: number;
}
```

- [ ] **Step 2: Add web API functions**

In `web/src/api.ts`, add:

```ts
export async function listProjects(): Promise<Project[]> {
  const response = await fetch(`${API_BASE}/projects`);
  if (!response.ok) throw new Error(await errorMessage(response));
  const data = await response.json();
  return data.projects ?? [];
}

export async function createProject(input: { name: string; slug?: string; description?: string }): Promise<Project> {
  const response = await fetch(`${API_BASE}/projects`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  return response.json();
}
```

Add `listConversations(projectId)` and `createConversation(projectId, input)`.

Extend stream request options:

```ts
export interface ProjectStreamContext {
  projectId: string;
  conversationId: string;
}
```

When `projectContext` is provided, include `project_id` and `conversation_id` in stream/resume payloads and do not include client `state`.

- [ ] **Step 3: Add App state**

In `App.tsx`, add:

```ts
const [projects, setProjects] = useState<Project[]>([]);
const [activeProjectId, setActiveProjectId] = useState('');
const [conversations, setConversations] = useState<Conversation[]>([]);
const [activeConversationId, setActiveConversationId] = useState('');
const projectMode = Boolean(activeProjectId && activeConversationId);
```

- [ ] **Step 4: Load projects and conversations**

Add effects:

```ts
useEffect(() => {
  listProjects().then(setProjects).catch((exc) => setError(exc instanceof Error ? exc.message : String(exc)));
}, []);

useEffect(() => {
  if (!activeProjectId) {
    setConversations([]);
    return;
  }
  listConversations(activeProjectId)
    .then(setConversations)
    .catch((exc) => setError(exc instanceof Error ? exc.message : String(exc)));
}, [activeProjectId]);
```

- [ ] **Step 5: Wire project context into chat stream**

Where `streamTask(...)` is called, pass:

```ts
const projectContext = projectMode
  ? { projectId: activeProjectId, conversationId: activeConversationId }
  : undefined;

await streamTask(
  prompt,
  target,
  reviewLevel,
  handlers,
  capabilityScope,
  projectMode ? null : runState,
  undefined,
  { signal, uploads: uploadsForRequest, projectContext },
);
```

Apply the same rule to review resume.

- [ ] **Step 6: Add navigation UI**

Add a compact project/conversation selector to the existing sidebar area:

- Project list.
- New project button.
- Conversation list.
- New conversation button.
- Busy state label when `active_kind` is present.

Use existing button, list, and panel styles where possible; do not add a marketing-style landing page.

- [ ] **Step 7: Verify web build**

Run:

```bash
npm --prefix web test
npm --prefix web run build
```

Expected: PASS.

- [ ] **Step 8: Commit WebUI project sessions**

```bash
git add web/src/types.ts web/src/api.ts web/src/App.tsx web/src/styles.css
git commit -m "feat: add project session UI"
```

## Task 8: Documentation And Final Verification

**Files:**
- Create or modify `docs/en/api-persistence.md`
- Create or modify `docs/zh-CN/api-persistence.md`
- Modify `docs/en/README.md`
- Modify `docs/zh-CN/README.md`
- Modify `examples/README.md` only if a runnable example is added

- [ ] **Step 1: Add English docs**

Create `docs/en/api-persistence.md` explaining:

- SDK remains persistence-free.
- API project mode owns persistence.
- Projects share one workspace.
- Conversations are single-writer sessions.
- Same-project concurrent conversations may conflict at the file level.
- Enterprise path uses Postgres, object storage, worker leases, and optional worktree isolation.

- [ ] **Step 2: Add Chinese docs**

Create `docs/zh-CN/api-persistence.md` with the same content in Simplified Chinese.

- [ ] **Step 3: Link docs**

Add links to:

- `docs/en/README.md`
- `docs/zh-CN/README.md`

- [ ] **Step 4: Run backend verification**

Run:

```bash
uv run --extra dev pytest tests/test_api_persistence.py tests/test_api.py tests/test_workspace_defaults.py tests/test_agent_sdk_public_api.py -q
```

Expected: PASS.

- [ ] **Step 5: Run full Python tests**

Run:

```bash
uv run --extra dev pytest
```

Expected: PASS.

- [ ] **Step 6: Run frontend verification**

Run:

```bash
npm --prefix web test
npm --prefix web run build
```

Expected: PASS.

- [ ] **Step 7: Run diff hygiene**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 8: Commit docs**

```bash
git add docs/en/api-persistence.md docs/zh-CN/api-persistence.md docs/en/README.md docs/zh-CN/README.md
git commit -m "docs: describe API persistence"
```

## Enterprise Follow-Up

The v1 design deliberately leaves these as follow-up implementation work:

- `api/storage/postgres.py` with connection pooling and JSONB columns.
- Worker claim/lease flow using `runs.lease_owner` and `runs.lease_expires_at`.
- Object storage implementation of `WorkspaceStore`.
- Optional per-conversation worktree mode for projects where concurrent file writes must be isolated.
- Merge/apply/review flow from per-conversation workspaces back into a project baseline.
- Authentication middleware that injects `org_id` and `user_id` into all store calls.

## Final Acceptance Criteria

- Existing local `/messages/stream` and `/messages/resume` behavior remains compatible.
- Project mode rejects client state and workspace root.
- Project mode stores and restores `RunState` from DB.
- Same conversation has single-writer locking.
- Same project can run multiple conversations concurrently.
- Project workspace path is used directly, without automatic run subdirectories.
- Awaiting review survives API process restart and can resume from DB state.
- Trace and artifact endpoints work from persisted state when runner memory is empty.
- SDK public surface remains persistence-free.
