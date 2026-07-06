# API Backend Persistence

The public Python SDK remains persistence-free. `Runner`, `ToolAgent`,
`DagAgent`, `Dag`, and `RunState` stay declarative/runtime objects; they do not
open databases, own users, or manage projects. Persistence belongs to the
FastAPI backend in `api/`.

## Local Project Mode

The Web UI and API backend support projects and conversations:

- A project owns one shared workspace directory:
  `.dagent/projects/<project_id>/workspace`.
- Standalone conversations use isolated directories under
  `.dagent/projects/_conversations/<conversation_id>/workspace`.
- A project can contain multiple conversations.
- The project directory is not globally locked.
- A single conversation is single-writer: one stream or resume can drive it at a
  time. A second writer receives `409`. Conversation locks are leases so a
  crashed process cannot leave a conversation permanently busy.
- Different conversations in the same project can run concurrently and may touch
  the same project files.

Project message streams use the existing `/messages/stream` endpoint with
`project_id` and `conversation_id`. In project mode the backend rejects client
`state` and `workspace_root`; it loads the previous `RunState` from the API
store and passes the project workspace to `Runner.stream(..., workspace_path=...)`.

## Stored Data

The local backend uses SQLite through `api/storage/`:

- `projects`: tenant-ready project metadata and `workspace_uri`.
- `conversations`: standalone and project chat sessions, owner metadata,
  workspace URI, conversation `kind`, and `last_run_id`. Chat, dynamic DAG,
  and static DAG conversations are separate kinds and are not reused across
  endpoints.
- `runs`: the current authoritative `RunState` snapshot for a run, with an
  optional saved DAG reference for static DAG runs.
- `run_streams`: one HTTP stream/resume execution attempt.
- `run_events`: durable SSE event history with database event ids.
- `reviews`: pending/resolved review metadata. Review state lives in
  `runs.state_json`, not in this table.
- `saved_dags`: saved static DAG specs, layout metadata, revisions, and project
  ownership.
- `orchestration_sessions`: dynamic/static orchestration editor state attached
  to a matching conversation kind.

Run artifacts are not duplicated in SQL. The backend derives run artifacts from
`RunState.trace` plus files in the workspace. Saved static DAG input uploads are
stored on disk under the API config directory so they survive process restarts
and can be materialized into future static DAG run workspaces.

The local SQLite schema is treated as an API/WebUI storage schema, not a public
SDK data contract. Incompatible pre-release local databases are recreated
instead of migrated with compatibility shims.

## Orchestration History

Orchestration history is managed through existing API persistence objects.
Dynamic orchestration history is stored as `dynamic_dag` conversations with
attached `orchestration_sessions` and runs. In the orchestration workspace,
dynamic orchestration sessions are standalone conversations and use standalone
conversation workspaces, not project workspaces. Project-scoped DAG
conversations remain part of the smart workbench project flow. Static
orchestration history is stored as `saved_dags` plus runs linked by
`saved_dag_id`.

The WebUI uses these endpoints to manage orchestration history:

```text
PATCH /conversations/{conversation_id}
PATCH /projects/{project_id}/conversations/{conversation_id}
GET /conversations/{conversation_id}/runs
GET /orchestration-sessions/{session_id}/runs
GET /saved-dags/{dag_id}/runs
```

## Resume And Restart Behavior

For review resume, use:

```text
POST /projects/{project_id}/reviews/{review_id}/resume
```

The backend reads `runs.state_json`, reconstructs `RunState`, and calls
`Runner.resume_stream(decision, state=run_state)`. The client does not send
state in hosted/project mode.

Trace and artifact endpoints read the stored `RunState` first and fall back to
the in-memory runner only when no database state exists. This lets completed and
awaiting-review project runs remain inspectable after an API process restart,
as long as the workspace files are still reachable.

Dynamic and static orchestration resume through `orchestration_sessions`.
Review resume updates the attached session draft from the final `RunState`, and
the WebUI restores orchestration drafts from the saved session when revisiting a
matching conversation.

## Enterprise Path

The abstractions are intentionally separable:

- `Store`: SQLite locally; Postgres for multi-instance/worker deployments.
- `WorkspaceStore`: local `file://` directories locally; object storage such as
  S3/GCS for server containers.
- Execution: local API-process execution now; queued worker execution later.

For server-side containers, do not rely on ephemeral container disks for project
files. Use a persistent volume for local deployments or object storage with
worker `sync_in`/`sync_out` for enterprise deployments.
