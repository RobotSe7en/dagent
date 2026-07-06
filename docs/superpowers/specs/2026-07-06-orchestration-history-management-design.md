# Orchestration History Management Design

## Goal

Add complete history management for the orchestration workspace without changing
the public Python SDK.

Dynamic orchestration history is managed as standalone `dynamic_dag`
conversations in the orchestration workspace. Static orchestration history is
managed as saved DAG assets. Project-scoped DAG conversations remain part of the
smart workbench project flow and are not shown or created by the dynamic
orchestration page.

## Current State

The backend already persists the primitives needed for this feature:

- `conversations` stores chat, dynamic DAG, and static DAG conversation records.
- `orchestration_sessions` stores dynamic/static editor drafts and UI state.
- `saved_dags` stores static DAG specs, layout, revision, and archive state.
- `runs`, `run_streams`, and `run_events` store run state snapshots and durable
  stream history.

The frontend currently exposes full management for chat conversations and
projects, and exposes a static DAG list in the orchestration sidebar. Dynamic
orchestration has no dedicated history list in the orchestration workspace.
Static DAGs can be created and updated, but the frontend does not expose saved
DAG deletion or run history. Both dynamic and static orchestration restore only
the most recent run through `conversation.last_run_id`.

## Non-Goals

- Do not add SDK persistence or modify `Runner`.
- Do not add a new `dynamic_dags` table.
- Do not store dynamic orchestration in `saved_dags`.
- Do not replace the existing project/conversation workspace model.
- Do not implement DAG revision diffing or event-sourced DAG playback in this
  iteration.

## Product Model

### Dynamic Orchestration

Dynamic orchestration is a conversation-backed workspace:

- The managed object is an `ApiConversation` with `kind == "dynamic_dag"`.
- Its draft graph and selected UI state live in the associated
  `OrchestrationSession`.
- Its historical runs are the `runs` rows attached to that conversation.
- Deleting a dynamic orchestration deletes the conversation and cascades through
  its orchestration session, runs, reviews, and run events.

Dynamic orchestration supports:

- List historical dynamic orchestration conversations.
- Search by title, id, and status.
- Create a new dynamic orchestration conversation and session.
- Rename a dynamic orchestration conversation.
- Delete a dynamic orchestration conversation.
- Select a dynamic orchestration and hydrate its draft and most recent run.
- View the run history for the selected dynamic orchestration.
- Select a historical run for read-only trace/output/artifact inspection without
  overwriting the current draft.

### Static Orchestration

Static orchestration is an asset-backed workspace:

- The managed object is a `SavedDag`.
- The editor draft is built from `SavedDag.spec` plus `SavedDag.layout`.
- Static DAG runs are tied to the saved DAG through `runs.saved_dag_id`.
- A static orchestration session still exists to remember the active saved DAG
  and editor UI state for a conversation.

Static orchestration supports:

- List saved DAG assets.
- Search by saved DAG id, name, description, spec id, revision, and node count.
- Create a new saved DAG.
- Edit graph, metadata, artifacts, and layout.
- Delete/archive a saved DAG.
- Select a saved DAG and hydrate the editor.
- View run history for the selected saved DAG.
- Select a historical run for read-only trace/output/artifact inspection without
  overwriting the editor draft.

## Backend Design

### Store Contract

Extend `api.storage.base.Store` and `api.storage.sqlite.SQLiteStore`.

Add conversation update:

```python
def update_conversation(
    self,
    conversation_id: str,
    *,
    title: str,
    org_id: str = "default",
) -> Conversation:
    pass
```

Extend conversation listing with optional kind filtering:

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
```

Extend run listing with optional saved DAG filtering:

```python
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

No schema migration is needed because the existing tables already contain the
required columns.

### API Requests

Add request models in `api/app.py`:

```python
class ConversationUpdateRequest(BaseModel):
    title: str
```

The existing `SavedDAGCreateRequest`, `SavedDAGUpdateRequest`, and
`OrchestrationSessionUpdateRequest` remain the source of truth for static DAG
and session edits.

### Conversation Management Endpoints

Add standalone conversation rename:

```text
PATCH /conversations/{conversation_id}
```

Behavior:

- Reject missing conversation with `404`.
- Reject project conversations with `400`; project conversations must use the
  project route.
- Validate non-empty title with `_clean_required_text`.
- Return a JSON object with one `conversation` key containing the serialized
  conversation payload.

Add project conversation rename:

```text
PATCH /projects/{project_id}/conversations/{conversation_id}
```

Behavior:

- Reject missing project with `404`.
- Reject missing conversation or project mismatch with `404`.
- Validate non-empty title.
- Return a JSON object with one `conversation` key containing the serialized
  conversation payload.

Add optional `kind` filter to existing list endpoints:

```text
GET /conversations?kind=dynamic_dag
GET /projects/{project_id}/conversations?kind=dynamic_dag
```

The filter accepts the existing `ConversationKind` values:
`chat`, `dynamic_dag`, and `static_dag`.

### Run History Endpoints

Add standalone conversation run history:

```text
GET /conversations/{conversation_id}/runs
```

Behavior:

- Reject missing conversation with `404`.
- Reject project conversations with `400`; project conversations must use the
  project route.
- Return a JSON object with one `runs` key containing `_run_summary_payload`
  entries.

Keep the existing project conversation run history:

```text
GET /projects/{project_id}/conversations/{conversation_id}/runs
```

Update it only as needed to share implementation with the standalone endpoint.

Add orchestration session run history:

```text
GET /orchestration-sessions/{session_id}/runs
```

Behavior:

- Reject missing session with `404`.
- List runs by `session.conversation_id`.
- Return summaries in `updated_at DESC` order, matching `Store.list_runs`.

Add saved DAG run history:

```text
GET /saved-dags/{dag_id}/runs
```

Behavior:

- Reject missing or archived saved DAG with `404`.
- List runs where `saved_dag_id == dag_id`.
- Return summaries in `updated_at DESC` order.

### Saved DAG Deletion

The backend already exposes:

```text
DELETE /saved-dags/{dag_id}
```

Keep the current archive behavior. It clears `saved_dag_id` references from
orchestration sessions and removes saved DAG artifact uploads. It does not
delete historical runs. Historical runs remain inspectable through run ids if
their workspace files still exist.

## Frontend Design

### API Layer

Extend `web/src/api.ts` with typed helpers:

- `updateConversation(conversationId, { title })`
- `updateProjectConversation(projectId, conversationId, { title })`
- `listConversations({ kind })`
- `listProjectConversations(projectId, { kind })`
- `listConversationRuns(conversationId)`
- `listProjectConversationRuns(projectId, conversationId)`
- `listOrchestrationSessionRuns(sessionId)`
- `listSavedDagRuns(savedDagId)`
- `deleteSavedDag(savedDagId)`

Keep helper signatures explicit. Add optional arguments only when the current
caller needs an optional behavior, such as a visible message projection field
for dynamic orchestration prompts.

Add frontend types:

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

### App State

Add focused state to `App.tsx`:

- `dynamicConversationQuery`
- `dynamicConversationEditTarget`
- `dynamicConversationDeleteTarget`
- `dynamicRunHistory`
- `dynamicSelectedRunId`
- `dynamicRunHistoryLoading`
- `dynamicRunHistoryError`
- `staticDagDeleteTarget`
- `staticRunHistory`
- `staticSelectedRunId`
- `staticRunHistoryLoading`
- `staticRunHistoryError`

Do not split `App.tsx` in this iteration. It is already the local pattern for
this UI. Extract only small pure helpers if needed for readability.

### Dynamic Orchestration Sidebar

When `activeWorkspace === "orchestration"` and
`orchestrationMode === "dynamic"`, render a dynamic orchestration list.

List source:

- Use `conversations` already loaded by the app.
- Filter to `conversation.kind === "dynamic_dag"` and `project_id == null`.
- Do not respect the selected project context; the dynamic orchestration page is
  independent from project workspaces.
- Search title, id, and status.

Actions:

- New: create a standalone `dynamic_dag` conversation and orchestration session,
  then clear the dynamic workspace state.
- Select: hydrate the conversation using the existing
  `hydrateOrchestrationConversation`.
- Rename: call the new conversation update helper, refresh local conversation
  state, and preserve selection.
- Delete: call the existing conversation delete helper, remove the conversation
  from state, clear the workspace if it was selected, and select the next
  dynamic conversation when one exists.

### Dynamic Run History Panel

For the selected dynamic orchestration session:

- Load `GET /orchestration-sessions/{session_id}/runs`.
- Display run id, status, updated time, and a short output preview.
- Mark `conversation.last_run_id` as latest.
- Selecting a run loads `GET /runs/{run_id}/events`, reconstructs the result
  with existing persisted-run helpers, and displays trace/output/artifacts.
- Selecting a historical run does not patch `orchestration_sessions.draft_dag`.
- Running the dynamic orchestration always uses the current draft and active
  conversation, not the selected historical run.
- Generated DAG review happens in the dynamic orchestration canvas. Clicking
  Run resumes the pending DAG review as approved and starts execution. The
  global DAG review dialog remains only for smart workbench DAG conversations.

### Static DAG Sidebar

Keep the existing saved DAG list and add row actions:

- New: existing `onNewDag`.
- Select/edit: existing `onLoadDag` and editor save flow.
- Delete: new `deleteSavedDag` helper, confirmation dialog, local saved DAG
  state removal, editor clear when deleting the active saved DAG.

Deletion behavior:

- If the deleted DAG is active, clear `editorSavedDagId`, reset revision/layout
  metadata, and load an empty static DAG.
- If another saved DAG exists, selecting the next one is allowed but not
  automatic. Clearing the editor is less surprising after destructive action.

### Static Run History Panel

For the selected saved DAG:

- Load `GET /saved-dags/{dag_id}/runs`.
- Display run id, status, updated time, and a short output preview.
- Selecting a run loads events, reconstructs trace/output, and displays
  artifacts in the existing run artifact panel.
- Selecting a historical run does not mutate the saved DAG editor draft.
- Running the static DAG still saves the current editor draft before execution.

## Error Handling

- Rename with an empty title shows the backend validation message.
- Deleting an already deleted conversation or saved DAG refreshes the list and
  clears selection if needed.
- Run history load failures show an inline error in the history panel.
- Historical run event load failures do not clear the current editor draft.
- Project-scoped conversation updates must use the project route; standalone
  routes reject project conversations.

## Testing

### Backend

Add tests to `tests/test_api_persistence.py`:

- SQLite conversation update persists and updates `updated_at`.
- `list_conversations(kind=ConversationKind.dynamic_dag)` filters standalone and
  project records.
- `list_runs(saved_dag_id="saved_dag_1")` filters static DAG runs.
- `PATCH /conversations/{id}` renames standalone conversations.
- `PATCH /projects/{project_id}/conversations/{id}` renames project
  conversations.
- Standalone run history endpoint returns conversation runs.
- Orchestration session run history returns dynamic/static session runs.
- Saved DAG run history returns only runs for that saved DAG.

### Frontend

Add tests near the existing web test style:

- `web/src/api.ts` helpers build the expected endpoints.
- Dynamic orchestration list filters only `dynamic_dag` conversations.
- Static saved DAG delete removes an item from the list without affecting other
  saved DAGs.

If no established React component test harness exists for `App.tsx`, keep the
first frontend increment focused on API helpers and pure list/filter helpers,
then verify the browser behavior manually.

### Manual Verification

Run:

```bash
uv run --extra dev pytest tests/test_api_persistence.py
npm --prefix web test
npm --prefix web run build
git diff --check
```

Manual UI checks:

- Create, rename, select, and delete a dynamic orchestration.
- Confirm dynamic orchestration runs use a standalone conversation workspace,
  even when a project is selected elsewhere in the UI.
- Run dynamic orchestration twice and switch between history entries.
- Confirm smart workbench DAG conversations still use the DAG review dialog.
- Create, edit, run, and delete a static saved DAG.
- Run a static saved DAG twice and switch between history entries.
- Confirm normal chat conversations and project file browsing still work.

## Rollout

This is an additive API/WebUI change. Existing public SDK surfaces do not
change. Local API/WebUI SQLite storage gains the `conversation_messages` table;
pre-release local databases are not kept alive with compatibility shims or
legacy request-shape conversion.

Docs to update with implementation:

- `docs/en/api-backend-persistence.md`
- `docs/zh-CN/api-backend-persistence.md`
- `docs/en/results-streaming-review.md`
- `docs/zh-CN/results-streaming-review.md`

The root README does not need a new section unless the final UI behavior becomes
part of the public quick-start path.
