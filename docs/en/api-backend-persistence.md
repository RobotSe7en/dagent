# API Backend Persistence

The public Python SDK remains persistence-free. `Runner`, `ToolAgent`,
`DagAgent`, `Dag`, and `RunState` stay declarative/runtime objects; they do not
open databases, own users, or manage projects. Persistence belongs to the
FastAPI backend in `api/`.

## Local Project Mode

The Web UI and API backend support projects and conversations:

- A project owns one shared workspace directory:
  `.dagent/projects/<project_id>/workspace`.
- A project can contain multiple conversations.
- The project directory is not globally locked.
- A single conversation is single-writer: one stream or resume can drive it at a
  time. A second writer receives `409`.
- Different conversations in the same project can run concurrently and may touch
  the same project files.

Project message streams use the existing `/messages/stream` endpoint with
`project_id` and `conversation_id`. In project mode the backend rejects client
`state` and `workspace_root`; it loads the previous `RunState` from the API
store and passes the project workspace to `Runner.stream(..., workspace_path=...)`.

## Stored Data

The local backend uses SQLite through `api/storage/`:

- `projects`: tenant-ready project metadata and `workspace_uri`.
- `conversations`: project chat sessions and `last_run_id`.
- `runs`: the current authoritative `RunState` snapshot for a run.
- `run_streams`: one HTTP stream/resume execution attempt.
- `run_events`: durable SSE event history with database event ids.
- `reviews`: pending/resolved review metadata. Review state lives in
  `runs.state_json`, not in this table.

Artifacts are not duplicated in SQL. The backend derives artifacts from
`RunState.trace` plus files in the workspace.

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

## Enterprise Path

The abstractions are intentionally separable:

- `Store`: SQLite locally; Postgres for multi-instance/worker deployments.
- `WorkspaceStore`: local `file://` directories locally; object storage such as
  S3/GCS for server containers.
- Execution: local API-process execution now; queued worker execution later.

For server-side containers, do not rely on ephemeral container disks for project
files. Use a persistent volume for local deployments or object storage with
worker `sync_in`/`sync_out` for enterprise deployments.
