# API Backend Persistence

> Version note: this page describes the bundled local host implementation. For
> the contract required of other hosts, see
> [Host migration for 0.8](host-migration-0.8.md).

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
- Saved static DAG definitions are not project or conversation resources. Each
  explicit static run gets its own
  `.dagent/projects/_runs/<run_id>/workspace` and does not acquire a
  conversation lock.

Message streams use `/messages/stream` with one `input` string and a
`conversation_id`; project conversations also include `project_id`. The
backend rejects the removed client `messages` and `state` fields, loads the
bounded `ConversationState` from the API store, and passes it to
`Runner.stream(..., conversation=...)`. It supplies the host-owned conversation
or project workspace separately.

## Stored Data

The local backend uses SQLite through `api/storage/`:

- `projects`: tenant-ready project metadata and `workspace_uri`.
- `conversations`: standalone and project chat sessions, owner metadata,
  workspace URI, conversation `kind`, `last_run_id`, and the complete bounded
  `ConversationState` plus its compare-and-swap revision. Chat, dynamic DAG,
  and static DAG conversations are separate kinds and are not reused across
  endpoints.
- `runs`: the current authoritative `RunState` snapshot for a run, the complete
  `RunCheckpoint` while review is pending, and an optional saved DAG reference
  for static DAG runs. Every ordinary turn gets a distinct run id.
- `run_streams`: one HTTP stream/resume execution attempt.
- `run_events`: durable SSE event history with database event ids.
- `conversation_messages`: visible user/assistant message timelines projected
  for chat conversations and dynamic DAG conversations that have an explicit
  `smart_workbench` or `orchestration_workspace` surface.
- `reviews`: pending/resuming/resolved review metadata. The review row is
  claimed atomically; resumable execution state lives in `runs.checkpoint_json`.
- `saved_dags`: reusable static DAG specs, layout metadata, and revisions.
- `orchestration_sessions`: conversation-backed dynamic orchestration editor
  state. Legacy static sessions remain readable so their existing runs and
  reviews can be resumed.

Run artifacts are not duplicated in SQL. The backend derives run artifacts from
`RunState.trace` plus files in the workspace. Saved static DAG input uploads are
stored on disk under the API config directory so they survive process restarts
and can be materialized into future static DAG run workspaces.

The local SQLite schema is treated as an API/WebUI storage schema, not a public
SDK data contract. The 0.8 columns are added with an explicit SQLite schema
migration; SDK payload versions are still validated strictly rather than
converted at runtime.

## Orchestration History

Orchestration history is managed through existing API persistence objects.
Dynamic orchestration history is stored as `dynamic_dag` conversations with
attached `orchestration_sessions` and runs. In the orchestration workspace,
dynamic orchestration sessions are standalone conversations and use standalone
conversation workspaces, not project workspaces. Project-scoped DAG
conversations remain part of the smart workbench project flow. Static
orchestration history is stored as `saved_dags` plus runs linked by
`saved_dag_id`. Every new static run has a distinct run id and workspace; its
`project_id` and `conversation_id` are null.

Create a reusable definition and start it with these project-neutral request
shapes:

```text
POST /saved-dags
{ "name": "Report", "spec": { ... }, "layout": { ... } }

POST /saved-dags/{dag_id}/run/stream
{ "graph_input": { ... } }
```

The WebUI uses these endpoints to manage orchestration history:

```text
PATCH /conversations/{conversation_id}
PATCH /projects/{project_id}/conversations/{conversation_id}
GET /conversations/{conversation_id}/runs
GET /orchestration-sessions/{session_id}/runs
GET /saved-dags/{dag_id}/runs
DELETE /runs/{run_id}
```

`DELETE /runs/{run_id}` removes the run history entry. It deletes the run row,
stream/event/state records, review records for that run, any dedicated run
workspace, and visible `conversation_messages` whose `run_id` matches the
deleted run. Awaiting-review runs can be deleted; doing so intentionally
discards the pending review and the visible transcript for that run.

## Resume And Restart Behavior

For persisted review resume, use:

```text
POST /projects/{project_id}/reviews/{review_id}/resume
POST /reviews/{review_id}/resume
```

The project route applies to conversation-backed project runs. New static DAG
runs use the project-neutral route. Both load the authoritative persisted
checkpoint; a static resume reuses the run-owned workspace and never creates or
locks a conversation.

The backend atomically changes the review from `pending` to `resuming`, loads
the complete `RunCheckpoint` from the associated run, and calls
`Runner.resume_stream(decision, checkpoint=checkpoint)`. The client never sends
conversation state or checkpoint data. A non-persisted `/messages/resume`
request identifies an in-memory checkpoint by `run_id`.

When a stream reaches another review gate, the replacement checkpoint is
stored. Completion, rejection, cancellation, and failure clear it. This makes
duplicate resume attempts conflict instead of executing the reviewed
capability twice.

Trace and artifact endpoints read the stored `RunState` first and fall back to
the in-memory runner only when no database state exists. This lets completed and
awaiting-review project runs remain inspectable after an API process restart,
as long as the workspace files are still reachable.

Dynamic orchestration resumes through `orchestration_sessions`. Static review
resume is keyed by the saved run and checkpoint. Existing conversation-backed
static runs retain their legacy resume behavior.

## Enterprise Path

The abstractions are intentionally separable:

- `Store`: SQLite locally; Postgres for multi-instance/worker deployments.
- `WorkspaceStore`: local `file://` directories locally; object storage such as
  S3/GCS for server containers.
- Execution: local API-process execution now; queued worker execution later.

For server-side containers, do not rely on ephemeral container disks for project
files. Use a persistent volume for local deployments or object storage with
worker `sync_in`/`sync_out` for enterprise deployments.
