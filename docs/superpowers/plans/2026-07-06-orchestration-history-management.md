# Orchestration History Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add full dynamic orchestration conversation management and static saved DAG management/history in the API and WebUI without changing the Python SDK.

**Architecture:** The API remains the persistence boundary. Dynamic orchestration is managed through `Conversation(kind="dynamic_dag")` plus `OrchestrationSession`; static orchestration is managed through `SavedDag` plus run history filtered by `runs.saved_dag_id`. The frontend adds typed API helpers and orchestration sidebar/history UI while preserving existing chat/project flows.

**Tech Stack:** FastAPI, SQLite, Pydantic, React, TypeScript, Vite, existing dagent schemas and WebUI state patterns.

---

## File Map

- Modify `api/storage/base.py`: extend the storage protocol for conversation updates, conversation kind filtering, and saved DAG run filtering.
- Modify `api/storage/sqlite.py`: implement the storage protocol additions.
- Modify `api/app.py`: add request model, conversation patch endpoints, optional kind query parameters, standalone conversation runs, orchestration session runs, and saved DAG runs.
- Modify `tests/test_api_persistence.py`: add backend persistence and API tests.
- Modify `web/src/types.ts`: add `ApiRunSummary`.
- Modify `web/src/api.ts`: add API helpers for conversation updates, run history, and saved DAG deletion.
- Modify `web/src/App.tsx`: add dynamic orchestration list management, static saved DAG deletion, and run history panels.
- Modify `web/src/styles.css`: style orchestration list row actions and run history panel.
- Modify `web/scripts/schemaArguments.test.mjs` or create a focused web utility test if helper extraction is needed.
- Modify docs pages after implementation: `docs/en/api-backend-persistence.md`, `docs/zh-CN/api-backend-persistence.md`, `docs/en/results-streaming-review.md`, `docs/zh-CN/results-streaming-review.md`.

---

### Task 1: Backend Storage Contract

**Files:**
- Modify: `api/storage/base.py`
- Modify: `api/storage/sqlite.py`
- Test: `tests/test_api_persistence.py`

- [ ] **Step 1: Write failing storage tests**

Add tests near the existing SQLite persistence tests:

```python
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
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run --extra dev pytest tests/test_api_persistence.py \
  -k "updates_conversation_title or filters_conversations_by_kind or filters_runs_by_saved_dag" -q
```

Expected: tests fail because `update_conversation`, `kind`, and `saved_dag_id`
support are not implemented.

- [ ] **Step 3: Extend `Store` protocol**

In `api/storage/base.py`, update the protocol signatures:

```python
    def list_conversations(
        self,
        project_id: str | None = None,
        *,
        standalone: bool = False,
        org_id: str | None = None,
        kind: ConversationKind | None = None,
    ) -> list[Conversation]:
        pass

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str,
        org_id: str = "default",
    ) -> Conversation:
        pass

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        conversation_id: str | None = None,
        saved_dag_id: str | None = None,
        org_id: str | None = None,
    ) -> list[Run]:
        pass
```

- [ ] **Step 4: Implement SQLite methods**

In `api/storage/sqlite.py`, update `list_conversations`:

```python
    def list_conversations(
        self,
        project_id: str | None = None,
        *,
        standalone: bool = False,
        org_id: str | None = None,
        kind: ConversationKind | None = None,
    ) -> list[Conversation]:
        conditions = ["archived_at IS NULL"]
        params: list[object] = []
        if standalone:
            conditions.append("project_id IS NULL")
        elif project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        if org_id is not None:
            conditions.append("org_id = ?")
            params.append(org_id)
        if kind is not None:
            conditions.append("kind = ?")
            params.append(kind)
        query = "SELECT * FROM conversations WHERE " + " AND ".join(conditions)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [_conversation_from_row(row) for row in rows]
```

Add `update_conversation`:

```python
    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str,
        org_id: str = "default",
    ) -> Conversation:
        now = _now()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE conversations
                SET title = ?, updated_at = ?
                WHERE id = ? AND org_id = ? AND archived_at IS NULL
                """,
                (title, now, conversation_id, org_id),
            )
            if cursor.rowcount == 0:
                self._conn.rollback()
                raise KeyError(f"Conversation '{conversation_id}' not found.")
            self._conn.commit()
            row = self._required_row("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        return _conversation_from_row(row)
```

Update `list_runs` by adding saved DAG filtering:

```python
        if saved_dag_id is not None:
            conditions.append("saved_dag_id = ?")
            params.append(saved_dag_id)
```

- [ ] **Step 5: Run storage tests**

Run:

```bash
uv run --extra dev pytest tests/test_api_persistence.py \
  -k "updates_conversation_title or filters_conversations_by_kind or filters_runs_by_saved_dag" -q
```

Expected: selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add api/storage/base.py api/storage/sqlite.py tests/test_api_persistence.py
git commit -m "feat: add orchestration history storage filters"
```

---

### Task 2: Backend API Endpoints

**Files:**
- Modify: `api/app.py`
- Modify: `tests/test_api_persistence.py`

- [ ] **Step 1: Write failing API tests**

Add tests near the existing API persistence endpoint tests:

```python
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
            "name": "Runnable",
            "spec": {"id": "runnable", "name": "Runnable", "nodes": [], "edges": []},
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
```

- [ ] **Step 2: Run API tests to verify failure**

Run:

```bash
uv run --extra dev pytest tests/test_api_persistence.py \
  -k "renames_standalone_dynamic or renames_project_dynamic or lists_standalone_conversation_runs or lists_orchestration_session_runs or lists_saved_dag_runs" -q
```

Expected: selected tests fail because endpoints are missing.

- [ ] **Step 3: Add request model**

In `api/app.py`, near `ConversationCreateRequest`, add:

```python
class ConversationUpdateRequest(BaseModel):
    title: str
```

- [ ] **Step 4: Add conversation list filters**

Change existing list endpoint signatures:

```python
@app.get("/conversations")
async def list_conversations(kind: ConversationKind | None = None) -> dict[str, Any]:
    conversations = await run_in_threadpool(
        state.get_store().list_conversations,
        standalone=True,
        kind=kind,
    )
    return {"conversations": [conversation.model_dump(mode="json") for conversation in conversations]}


@app.get("/projects/{project_id}/conversations")
async def list_project_conversations(project_id: str, kind: ConversationKind | None = None) -> dict[str, Any]:
    project = await run_in_threadpool(state.get_store().get_project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    conversations = await run_in_threadpool(
        state.get_store().list_conversations,
        project_id,
        kind=kind,
    )
    return {"conversations": [conversation.model_dump(mode="json") for conversation in conversations]}
```

- [ ] **Step 5: Add conversation patch endpoints**

Add helper:

```python
async def _update_conversation_title(conversation: Conversation, title: str) -> Conversation:
    clean_title = _clean_required_text(title, field="Conversation title")
    try:
        return await run_in_threadpool(
            state.get_store().update_conversation,
            conversation.id,
            title=clean_title,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found.") from exc
```

Add standalone endpoint:

```python
@app.patch("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, request: ConversationUpdateRequest) -> dict[str, Any]:
    conversation = await run_in_threadpool(state.get_store().get_conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conversation.project_id is not None:
        raise HTTPException(status_code=400, detail="Project conversations must be updated through the project route.")
    updated = await _update_conversation_title(conversation, request.title)
    return {"conversation": updated.model_dump(mode="json")}
```

Add project endpoint:

```python
@app.patch("/projects/{project_id}/conversations/{conversation_id}")
async def update_project_conversation(
    project_id: str,
    conversation_id: str,
    request: ConversationUpdateRequest,
) -> dict[str, Any]:
    project = await run_in_threadpool(state.get_store().get_project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    conversation = await run_in_threadpool(state.get_store().get_conversation, conversation_id)
    if conversation is None or conversation.project_id != project.id:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    updated = await _update_conversation_title(conversation, request.title)
    return {"conversation": updated.model_dump(mode="json")}
```

- [ ] **Step 6: Add run history endpoints**

Add shared helper:

```python
async def _conversation_run_summaries(
    conversation: Conversation,
    *,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    runs = await run_in_threadpool(
        state.get_store().list_runs,
        project_id=project_id,
        conversation_id=conversation.id,
    )
    return [_run_summary_payload(run) for run in runs]
```

Add standalone endpoint:

```python
@app.get("/conversations/{conversation_id}/runs")
async def list_conversation_runs(conversation_id: str) -> dict[str, Any]:
    conversation = await run_in_threadpool(state.get_store().get_conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conversation.project_id is not None:
        raise HTTPException(status_code=400, detail="Project conversation runs must be listed through the project route.")
    return {"runs": await _conversation_run_summaries(conversation)}
```

Add orchestration session endpoint:

```python
@app.get("/orchestration-sessions/{session_id}/runs")
async def list_orchestration_session_runs(session_id: str) -> dict[str, Any]:
    session = await run_in_threadpool(state.get_store().get_orchestration_session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Orchestration session not found.")
    runs = await run_in_threadpool(
        state.get_store().list_runs,
        project_id=session.project_id,
        conversation_id=session.conversation_id,
    )
    return {"runs": [_run_summary_payload(run) for run in runs]}
```

Add saved DAG endpoint:

```python
@app.get("/saved-dags/{dag_id}/runs")
async def list_saved_dag_runs(dag_id: str) -> dict[str, Any]:
    saved = await run_in_threadpool(state.get_store().get_saved_dag, dag_id)
    if saved is None:
        raise HTTPException(status_code=404, detail="Saved DAG not found.")
    runs = await run_in_threadpool(
        state.get_store().list_runs,
        project_id=saved.project_id,
        saved_dag_id=saved.id,
    )
    return {"runs": [_run_summary_payload(run) for run in runs]}
```

- [ ] **Step 7: Run API tests**

Run:

```bash
uv run --extra dev pytest tests/test_api_persistence.py \
  -k "renames_standalone_dynamic or renames_project_dynamic or lists_standalone_conversation_runs or lists_orchestration_session_runs or lists_saved_dag_runs" -q
```

Expected: selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add api/app.py tests/test_api_persistence.py
git commit -m "feat: add orchestration history api"
```

---

### Task 3: Frontend API Helpers

**Files:**
- Modify: `web/src/types.ts`
- Modify: `web/src/api.ts`
- Test: `web/scripts/schemaArguments.test.mjs` if the repository keeps script-based tests only; otherwise create `web/src/api.test.ts` following the active test runner.

- [ ] **Step 1: Add `ApiRunSummary` type**

In `web/src/types.ts`, add:

```ts
export interface ApiRunSummary {
  id: string;
  project_id?: string | null;
  conversation_id?: string | null;
  kind?: string | null;
  status: string;
  execution: 'local' | 'sandbox' | 'worker';
  workspace_uri: string;
  saved_dag_id?: string | null;
  output_text: string;
  has_state: boolean;
  has_error: boolean;
  created_at: number;
  started_at?: number | null;
  completed_at?: number | null;
  updated_at: number;
}
```

- [ ] **Step 2: Add helper functions in `api.ts`**

Import `ApiRunSummary` from `./types`, then add:

```ts
export async function updateConversation(
  conversationId: string,
  input: { title: string },
): Promise<ApiConversation> {
  const res = await fetch(`${API_BASE}/conversations/${encodeURIComponent(conversationId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.conversation;
}

export async function updateProjectConversation(
  projectId: string,
  conversationId: string,
  input: { title: string },
): Promise<ApiConversation> {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/conversations/${encodeURIComponent(conversationId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.conversation;
}

export async function listConversationRuns(conversationId: string): Promise<ApiRunSummary[]> {
  const res = await fetch(`${API_BASE}/conversations/${encodeURIComponent(conversationId)}/runs`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.runs ?? [];
}

export async function listProjectConversationRuns(
  projectId: string,
  conversationId: string,
): Promise<ApiRunSummary[]> {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/conversations/${encodeURIComponent(conversationId)}/runs`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.runs ?? [];
}

export async function listOrchestrationSessionRuns(sessionId: string): Promise<ApiRunSummary[]> {
  const res = await fetch(`${API_BASE}/orchestration-sessions/${encodeURIComponent(sessionId)}/runs`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.runs ?? [];
}

export async function listSavedDagRuns(savedDagId: string): Promise<ApiRunSummary[]> {
  const res = await fetch(`${API_BASE}/saved-dags/${encodeURIComponent(savedDagId)}/runs`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.runs ?? [];
}

export async function deleteSavedDag(savedDagId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/saved-dags/${encodeURIComponent(savedDagId)}`, {
    method: 'DELETE',
  });
  if (!res.ok) throw new Error(await errorMessage(res));
}
```

Update existing list helpers to accept optional kind:

```ts
export async function listConversations(options: { kind?: ApiConversation['kind'] } = {}): Promise<ApiConversation[]> {
  const params = new URLSearchParams();
  if (options.kind) params.set('kind', options.kind);
  const suffix = params.toString() ? `?${params.toString()}` : '';
  const res = await fetch(`${API_BASE}/conversations${suffix}`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.conversations ?? [];
}

export async function listProjectConversations(
  projectId: string,
  options: { kind?: ApiConversation['kind'] } = {},
): Promise<ApiConversation[]> {
  const params = new URLSearchParams();
  if (options.kind) params.set('kind', options.kind);
  const suffix = params.toString() ? `?${params.toString()}` : '';
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/conversations${suffix}`);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  return data.conversations ?? [];
}
```

- [ ] **Step 3: Run TypeScript build**

Run:

```bash
npm --prefix web run build
```

Expected: build passes with no TypeScript errors.

- [ ] **Step 4: Commit**

```bash
git add web/src/types.ts web/src/api.ts
git commit -m "feat: add orchestration history web api"
```

---

### Task 4: Dynamic Orchestration List Management

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`

- [ ] **Step 1: Add imports**

In `web/src/App.tsx`, import the new helpers:

```ts
  listOrchestrationSessionRuns,
  updateConversation,
  updateProjectConversation,
```

Import `ApiRunSummary`:

```ts
import type { ApiRunEvent, ApiRunResult, ApiRunState, ChatStreamMessage } from './api';
import type { ApiRunSummary } from './types';
```

If `ApiRunSummary` is imported from `./types`, keep the existing `./api` type
import unchanged and add `ApiRunSummary` to the `./types` import block.

- [ ] **Step 2: Add state**

Near existing dynamic orchestration state, add:

```ts
  const [dynamicConversationQuery, setDynamicConversationQuery] = useState('');
  const [dynamicConversationEditTargetId, setDynamicConversationEditTargetId] = useState('');
  const [dynamicConversationEditTitle, setDynamicConversationEditTitle] = useState('');
  const [dynamicConversationDeleteTargetId, setDynamicConversationDeleteTargetId] = useState('');
  const [dynamicRunHistory, setDynamicRunHistory] = useState<ApiRunSummary[]>([]);
  const [dynamicSelectedRunId, setDynamicSelectedRunId] = useState('');
  const [dynamicRunHistoryLoading, setDynamicRunHistoryLoading] = useState(false);
  const [dynamicRunHistoryError, setDynamicRunHistoryError] = useState<string | null>(null);
```

- [ ] **Step 3: Add filtered dynamic conversation helpers**

Near existing `visibleSavedDags`, add:

```ts
  const normalizedDynamicConversationQuery = normalizeSearchQuery(dynamicConversationQuery);
  const visibleDynamicConversations = conversations.filter((conversation) => {
    if (conversation.kind !== 'dynamic_dag') return false;
    if (selectedProjectId) {
      if (conversation.project_id !== selectedProjectId) return false;
    } else if (conversation.project_id) {
      return false;
    }
    return matchesSearchQuery(
      [conversation.id, conversation.title, conversation.status],
      normalizedDynamicConversationQuery,
    );
  });
```

- [ ] **Step 4: Add dynamic new/select/rename/delete handlers**

Add these handlers near existing orchestration handlers:

```ts
  const clearDynamicWorkspace = () => {
    syncDynamicDag(emptyDag);
    setDynamicTrace([]);
    setDynamicMessages([]);
    setDynamicFinalAnswer('');
    setDynamicMessage('');
    setDynamicRunState(null);
    setDynamicRunHistory([]);
    setDynamicSelectedRunId('');
    setDynamicStatusMessage('');
  };

  const createDynamicOrchestration = async () => {
    if (dynamicRunning || editorRunning) return;
    const context = await ensureOrchestrationContext(
      'dynamic_dag',
      '动态编排',
      {
        targetProjectId: selectedProjectId || null,
        draftDag: null,
        uiState: {},
      },
    );
    if (!context) return;
    clearDynamicWorkspace();
    setSelectedConversationId(context.conversation.id);
    if (context.conversation.project_id) setSelectedProjectId(context.conversation.project_id);
    await refreshConversations();
  };

  const selectDynamicOrchestration = async (conversationId: string) => {
    if (dynamicRunning || editorRunning) return;
    const conversation = conversationsRef.current.find((item) => item.id === conversationId);
    if (!conversation || conversation.kind !== 'dynamic_dag') return;
    setSelectedConversationId(conversation.id);
    if (conversation.project_id) setSelectedProjectId(conversation.project_id);
    clearDynamicWorkspace();
    await hydrateOrchestrationConversation(conversation);
  };

  const saveDynamicConversationTitle = async () => {
    const conversation = conversations.find((item) => item.id === dynamicConversationEditTargetId);
    const title = dynamicConversationEditTitle.trim();
    if (!conversation || !title) return;
    try {
      const updated = conversation.project_id
        ? await updateProjectConversation(conversation.project_id, conversation.id, { title })
        : await updateConversation(conversation.id, { title });
      setConversations((items) => items.map((item) => (item.id === updated.id ? updated : item)));
      setDynamicConversationEditTargetId('');
      setDynamicConversationEditTitle('');
    } catch (exc) {
      setDynamicStatusMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const deleteDynamicOrchestration = async () => {
    const conversation = conversations.find((item) => item.id === dynamicConversationDeleteTargetId);
    if (!conversation) return;
    try {
      await deleteConversationOnce(conversation);
      setConversations((items) => items.filter((item) => item.id !== conversation.id));
      setDynamicConversationDeleteTargetId('');
      if (selectedConversationId === conversation.id) {
        setSelectedConversationId('');
        clearDynamicWorkspace();
      }
    } catch (exc) {
      setDynamicStatusMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };
```

- [ ] **Step 5: Render dynamic list sidebar**

In the sidebar render area, add a section for dynamic mode before the static
saved DAG section:

```tsx
      {activeWorkspace === 'orchestration' && orchestrationMode === 'dynamic' ? (
        <section className="sidebar-context-section">
          <div className="sidebar-history-head">
            <span>动态编排</span>
            <button onClick={createDynamicOrchestration} title="新建动态编排" type="button">
              <Plus size={14} />
            </button>
          </div>
          <SidebarSearchField
            value={dynamicConversationQuery}
            onChange={setDynamicConversationQuery}
          />
          <div className="sidebar-context-list">
            {visibleDynamicConversations.length ? visibleDynamicConversations.map((conversation) => (
              <div
                className={conversation.id === selectedConversationId ? 'sidebar-conversation-row active' : 'sidebar-conversation-row'}
                key={conversation.id}
              >
                <button onClick={() => void selectDynamicOrchestration(conversation.id)} type="button">
                  <span>
                    <Play size={13} />
                    <strong>{conversation.title}</strong>
                  </span>
                  <em>{conversation.status}</em>
                </button>
                <button
                  className="sidebar-conversation-delete"
                  onClick={() => {
                    setDynamicConversationEditTargetId(conversation.id);
                    setDynamicConversationEditTitle(conversation.title);
                  }}
                  title="重命名"
                  type="button"
                >
                  <FileText size={12} />
                </button>
                <button
                  className="sidebar-conversation-delete"
                  onClick={() => setDynamicConversationDeleteTargetId(conversation.id)}
                  title="删除"
                  type="button"
                >
                  <Trash2 size={12} />
                </button>
              </div>
            )) : (
              <div className="sidebar-empty-row">
                {normalizedDynamicConversationQuery ? '没有匹配的动态编排' : '暂无动态编排'}
              </div>
            )}
          </div>
        </section>
      ) : null}
```

- [ ] **Step 6: Add rename and delete dialogs**

Add two modal components near the existing conversation/project dialogs:

```tsx
function DynamicConversationRenameDialog({
  title,
  value,
  onChange,
  onClose,
  onSave,
}: {
  title: string;
  value: string;
  onChange: (value: string) => void;
  onClose: () => void;
  onSave: () => void;
}) {
  return (
    <div className="modal-backdrop">
      <div className="modal-panel compact-modal">
        <h3>{title}</h3>
        <input value={value} onChange={(event) => onChange(event.target.value)} />
        <div className="modal-actions">
          <button className="secondary-button" onClick={onClose} type="button">取消</button>
          <button className="primary-button" onClick={onSave} type="button">保存</button>
        </div>
      </div>
    </div>
  );
}
```

Use the existing delete dialog pattern for delete confirmation. If existing
dialog components are project/conversation-specific, add a small
`DynamicConversationDeleteDialog` with confirm text and buttons.

- [ ] **Step 7: Run frontend build**

Run:

```bash
npm --prefix web run build
```

Expected: build passes.

- [ ] **Step 8: Commit**

```bash
git add web/src/App.tsx web/src/styles.css
git commit -m "feat: manage dynamic orchestration history"
```

---

### Task 5: Static Saved DAG Management and Run History

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`

- [ ] **Step 1: Add imports**

In `web/src/App.tsx`, import:

```ts
  deleteSavedDag,
  listSavedDagRuns,
```

- [ ] **Step 2: Add static state**

Near static editor state, add:

```ts
  const [staticDagDeleteTargetId, setStaticDagDeleteTargetId] = useState('');
  const [staticRunHistory, setStaticRunHistory] = useState<ApiRunSummary[]>([]);
  const [staticSelectedRunId, setStaticSelectedRunId] = useState('');
  const [staticRunHistoryLoading, setStaticRunHistoryLoading] = useState(false);
  const [staticRunHistoryError, setStaticRunHistoryError] = useState<string | null>(null);
```

- [ ] **Step 3: Add saved DAG delete handler**

Add near static DAG editor handlers:

```ts
  const confirmDeleteSavedDag = async () => {
    if (!staticDagDeleteTargetId) return;
    try {
      await deleteSavedDag(staticDagDeleteTargetId);
      setSavedDags((items) => items.filter((item) => item.id !== staticDagDeleteTargetId));
      if (editorSavedDagId === staticDagDeleteTargetId) {
        newEditorUserDag();
        setEditorMessage('已删除当前静态编排。');
      }
      setStaticDagDeleteTargetId('');
    } catch (exc) {
      setEditorMessage(exc instanceof Error ? exc.message : String(exc));
    }
  };
```

- [ ] **Step 4: Add saved DAG row delete action**

Replace static saved DAG list rows with row wrappers that include delete:

```tsx
              <div className={item.savedDagId === selectedDagId ? 'sidebar-saved-dag-row active' : 'sidebar-saved-dag-row'} key={item.savedDagId}>
                <button
                  onClick={() => onLoadDag(item)}
                  title={item.name || item.spec.name || item.spec.id}
                  type="button"
                >
                  <span>
                    <GitBranch size={13} />
                    <strong>{item.name || item.spec.name || item.spec.id}</strong>
                    <code>v{item.revision}</code>
                  </span>
                  <em>{item.description || item.spec.description || `${item.spec.nodes.length} 节点`}</em>
                </button>
                <button
                  className="sidebar-conversation-delete"
                  onClick={() => setStaticDagDeleteTargetId(item.savedDagId)}
                  title="删除静态编排"
                  type="button"
                >
                  <Trash2 size={12} />
                </button>
              </div>
```

- [ ] **Step 5: Add static run history loader**

Add effect:

```ts
  useEffect(() => {
    let cancelled = false;
    if (!editorSavedDagId) {
      setStaticRunHistory([]);
      setStaticRunHistoryError(null);
      setStaticRunHistoryLoading(false);
      return;
    }
    setStaticRunHistoryLoading(true);
    setStaticRunHistoryError(null);
    void listSavedDagRuns(editorSavedDagId)
      .then((runs) => {
        if (cancelled) return;
        setStaticRunHistory(runs);
      })
      .catch((exc) => {
        if (cancelled) return;
        setStaticRunHistory([]);
        setStaticRunHistoryError(exc instanceof Error ? exc.message : String(exc));
      })
      .finally(() => {
        if (!cancelled) setStaticRunHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [editorSavedDagId]);
```

- [ ] **Step 6: Add selected static run handler**

Add:

```ts
  const selectStaticRunHistory = async (runId: string) => {
    try {
      setStaticSelectedRunId(runId);
      const events = await listRunEvents(runId);
      const result = finishedRunResultFromEvents(events);
      const nextState = result?.state ?? null;
      if (nextState?.trace) {
        const traceEvents = mapRunTrace(nextState.trace);
        setEditorTrace(traceEvents);
        setEditorRunTimeline(runTranscriptFromTraceEvents(traceEvents));
      }
      if (nextState?.dag && nextState.trace && nextState.run_id) {
        setEditorRun({
          run_id: nextState.run_id,
          spec_id: nextState.spec_id ?? null,
          workspace_path: nextState.workspace_path ?? '',
          dag: nextState.dag,
          trace: nextState.trace,
          status: dagRunStatus(nextState.status),
        });
      }
    } catch (exc) {
      setStaticRunHistoryError(exc instanceof Error ? exc.message : String(exc));
    }
  };
```

- [ ] **Step 7: Render static run history panel**

Inside `OrchestrationWorkspace` props or adjacent static workspace render,
add a compact run history section:

```tsx
<RunHistoryPanel
  title="运行历史"
  runs={staticRunHistory}
  selectedRunId={staticSelectedRunId}
  loading={staticRunHistoryLoading}
  error={staticRunHistoryError}
  onSelectRun={(runId) => void selectStaticRunHistory(runId)}
/>
```

Create `RunHistoryPanel` in `App.tsx`:

```tsx
function RunHistoryPanel({
  title,
  runs,
  selectedRunId,
  loading,
  error,
  onSelectRun,
}: {
  title: string;
  runs: ApiRunSummary[];
  selectedRunId: string;
  loading: boolean;
  error: string | null;
  onSelectRun: (runId: string) => void;
}) {
  return (
    <section className="run-history-panel">
      <div className="run-history-head">
        <strong>{title}</strong>
        <span>{loading ? '加载中' : `${runs.length} 次运行`}</span>
      </div>
      {error ? <div className="sidebar-error-row">{error}</div> : null}
      <div className="run-history-list">
        {runs.map((run) => (
          <button
            className={run.id === selectedRunId ? 'active' : ''}
            key={run.id}
            onClick={() => onSelectRun(run.id)}
            type="button"
          >
            <span>{run.status}</span>
            <strong>{run.id}</strong>
            <em>{run.output_text || new Date(run.updated_at * 1000).toLocaleString()}</em>
          </button>
        ))}
      </div>
    </section>
  );
}
```

- [ ] **Step 8: Run frontend build**

Run:

```bash
npm --prefix web run build
```

Expected: build passes.

- [ ] **Step 9: Commit**

```bash
git add web/src/App.tsx web/src/styles.css
git commit -m "feat: manage static dag history"
```

---

### Task 6: Dynamic Run History Panel

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`

- [ ] **Step 1: Track active dynamic session id**

Add state next to the other dynamic orchestration state:

```ts
  const [dynamicOrchestrationSessionId, setDynamicOrchestrationSessionId] = useState('');
```

Inside `hydrateOrchestrationConversation`, set the active session id after a
dynamic session loads:

```ts
      if (session.kind === 'dynamic_dag') {
        setDynamicOrchestrationSessionId(session.id);
      }
```

When a non-dynamic session is hydrated through the same path, clear the active
dynamic session id:

```ts
      if (session.kind !== 'dynamic_dag') {
        setDynamicOrchestrationSessionId('');
      }
```

When clearing or deleting the selected dynamic conversation, also clear the id:

```ts
    setDynamicOrchestrationSessionId('');
```

- [ ] **Step 2: Load dynamic run history**

Add effect:

```ts
  useEffect(() => {
    let cancelled = false;
    if (!dynamicOrchestrationSessionId) {
      setDynamicRunHistory([]);
      setDynamicRunHistoryError(null);
      setDynamicRunHistoryLoading(false);
      return;
    }
    setDynamicRunHistoryLoading(true);
    setDynamicRunHistoryError(null);
    void listOrchestrationSessionRuns(dynamicOrchestrationSessionId)
      .then((runs) => {
        if (cancelled) return;
        setDynamicRunHistory(runs);
      })
      .catch((exc) => {
        if (cancelled) return;
        setDynamicRunHistory([]);
        setDynamicRunHistoryError(exc instanceof Error ? exc.message : String(exc));
      })
      .finally(() => {
        if (!cancelled) setDynamicRunHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [dynamicOrchestrationSessionId]);
```

- [ ] **Step 3: Add dynamic run selection**

Add:

```ts
  const selectDynamicRunHistory = async (runId: string) => {
    try {
      setDynamicSelectedRunId(runId);
      const events = await listRunEvents(runId);
      const result = finishedRunResultFromEvents(events);
      const nextState = result?.state ?? null;
      setDynamicRunState(nextState);
      if (nextState?.dag) syncDynamicDag(preserveDynamicDagEdges(nextState.dag));
      if (nextState?.trace) {
        setDynamicTrace(mapRunTrace(nextState.trace).map((event) => ({
          ...event,
          timelineOrder: nextDynamicTimelineOrder(),
        })));
      }
      if (result?.output_text) setOrderedDynamicFinalAnswer(result.output_text);
    } catch (exc) {
      setDynamicRunHistoryError(exc instanceof Error ? exc.message : String(exc));
    }
  };
```

- [ ] **Step 4: Render dynamic run history**

Pass the shared `RunHistoryPanel` to `DynamicOrchestrationWorkspace` or render it
adjacent to the dynamic workspace:

```tsx
<RunHistoryPanel
  title="运行历史"
  runs={dynamicRunHistory}
  selectedRunId={dynamicSelectedRunId}
  loading={dynamicRunHistoryLoading}
  error={dynamicRunHistoryError}
  onSelectRun={(runId) => void selectDynamicRunHistory(runId)}
/>
```

- [ ] **Step 5: Run frontend build**

Run:

```bash
npm --prefix web run build
```

Expected: build passes.

- [ ] **Step 6: Commit**

```bash
git add web/src/App.tsx web/src/styles.css
git commit -m "feat: show dynamic orchestration run history"
```

---

### Task 7: Documentation and Verification

**Files:**
- Modify: `docs/en/api-backend-persistence.md`
- Modify: `docs/zh-CN/api-backend-persistence.md`
- Modify: `docs/en/results-streaming-review.md`
- Modify: `docs/zh-CN/results-streaming-review.md`

- [ ] **Step 1: Update API persistence docs**

Add to both language versions:

```markdown
Orchestration history is managed through existing API persistence objects.
Dynamic orchestration history is stored as `dynamic_dag` conversations with
attached `orchestration_sessions` and runs. Static orchestration history is
stored as `saved_dags` plus runs linked by `saved_dag_id`.
```

Also document the new endpoints:

```text
PATCH /conversations/{conversation_id}
PATCH /projects/{project_id}/conversations/{conversation_id}
GET /conversations/{conversation_id}/runs
GET /orchestration-sessions/{session_id}/runs
GET /saved-dags/{dag_id}/runs
```

- [ ] **Step 2: Update results docs**

Add a short note to both language versions:

```markdown
The WebUI can inspect historical orchestration runs through persisted run
history. Selecting a historical run restores its trace/output/artifacts for
inspection without mutating the current dynamic draft or saved static DAG.
```

- [ ] **Step 3: Run backend tests**

Run:

```bash
uv run --extra dev pytest tests/test_api_persistence.py -q
```

Expected: tests pass.

- [ ] **Step 4: Run frontend tests**

Run:

```bash
npm --prefix web test
```

Expected: tests pass.

- [ ] **Step 5: Run frontend build**

Run:

```bash
npm --prefix web run build
```

Expected: build passes.

- [ ] **Step 6: Run diff check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 7: Manual browser verification**

Start the API/WebUI using the repository's normal local command. In the browser:

1. Open orchestration dynamic mode.
2. Create a dynamic orchestration.
3. Rename it.
4. Run it twice.
5. Select both run history entries and verify trace/output/artifacts update.
6. Delete the dynamic orchestration.
7. Open orchestration static mode.
8. Create and save a static DAG.
9. Run it twice.
10. Select both static run history entries and verify trace/output/artifacts update.
11. Delete the saved DAG.
12. Confirm normal chat conversations and project file browsing still work.

- [ ] **Step 8: Commit docs**

```bash
git add docs/en/api-backend-persistence.md docs/zh-CN/api-backend-persistence.md docs/en/results-streaming-review.md docs/zh-CN/results-streaming-review.md
git commit -m "docs: describe orchestration history management"
```

---

## Plan Self-Review

Spec coverage:

- Dynamic orchestration conversation list, create, rename, delete, select, and
  run history are covered by Tasks 2, 3, 4, and 6.
- Static saved DAG list, create/edit existing flow, delete, and run history are
  covered by Tasks 2, 3, and 5.
- SDK non-change is preserved because all changes are in `api/`, `web/`,
  `tests/`, and docs.
- Existing persistence model is preserved; no schema migration task is included.

Type consistency:

- Backend uses existing `ConversationKind`, `Conversation`, `Run`, and
  `_run_summary_payload`.
- Frontend uses `ApiConversation`, `SavedDag`, and the new `ApiRunSummary`.
- Run history selection reuses existing `listRunEvents`, `finishedRunResultFromEvents`,
  `mapRunTrace`, and run artifact refresh behavior.

Verification:

- Backend targeted tests are included before implementation steps.
- Frontend build is included after each UI/API increment.
- Full backend, frontend test, frontend build, diff check, and browser checks are
  included in the final task.
