# Smart Workbench Upload Design

Date: 2026-06-30
Branch: `feat/smart-workbench-upload`

## Context

The WebUI has two related file workflows:

- Static orchestration can upload files into DAG artifacts.
- The smart workbench can show run workspace files as artifacts, preview them,
  and download them.

The next project-management feature will introduce project work paths and
multiple chat sessions sharing project files. This change should not implement
project management yet, but it should avoid blocking that direction.

## Goals

- Support file and folder upload in both static orchestration and the smart
  workbench.
- Preserve uploaded folder directory trees.
- Keep upload path normalization and safety behavior consistent across both
  surfaces.
- Treat smart workbench uploads as current-session input files for the next run.
- Reuse the existing run workspace artifact browser for files after they are
  materialized.
- Leave a clear extension point for future project-level file storage.

## Non-Goals

- Do not implement project CRUD, project selection, or project persistence.
- Do not implement multi-session project history.
- Do not make the artifact drawer an upload entry point.
- Do not write uploaded smart workbench files into a long-lived project
  workspace yet.

## User Experience

### Smart Workbench

The upload entry point is the existing upload button in the lower-left area of
the text composer. The artifact drawer remains read-only for browsing,
previewing, refreshing, copying, and downloading run workspace files.

Users can select files or folders. Folder uploads preserve the browser-provided
relative paths. Before the next message is sent, the selected uploads are shown
as pending input files in the composer or adjacent input area. Removing a
pending file removes it from the next run request.

When the user sends the next message, the pending files are included with the
run request. The backend materializes them into the run workspace under:

```text
inputs/uploads/<uploaded relative path>
```

The run then sees these files through the normal workspace tools. After the run
starts, the files appear in the existing artifact drawer as run files and use
the existing preview and download endpoints.

For follow-up messages in the same chat session, the runtime already reuses the
existing `runState.workspace_path`. The uploaded files remain visible because
they live in that workspace.

Starting a new chat session clears pending smart workbench uploads and detaches
from the previous run workspace. Future project management can replace this
session-scoped storage with project-scoped storage.

### Static Orchestration

The existing Artifacts upload entry point remains the upload entry point for
static DAG input files. It should support folder selection and preserve
directory trees. Static orchestration continues to map uploaded files through
DAG artifact declarations.

Static orchestration and smart workbench use the same path normalization rules:
slashes are normalized to `/`, empty segments and `.` are removed, `..` escapes
are rejected, and unsafe filename characters are sanitized where the browser
filename is converted into an artifact path.

## Backend Design

### Upload Contracts

Add a smart workbench upload contract that accepts uploaded files with their
relative paths. The contract should model uploaded files as a typed request or
internal record rather than unstructured dictionaries.

The internal upload record should contain:

- `filename`: normalized relative path provided by the client.
- `content`: uploaded bytes.

This mirrors the existing `ArtifactUpload` shape and should be factored so both
static DAG artifact uploads and smart workbench uploads use one validation path.

### Smart Workbench Run Flow

`/messages/stream` should accept smart workbench uploads for the next run. On
run start, the backend writes them into the resolved run workspace before tool
or DAG execution begins.

Materialization rules:

- Destination root is `inputs/uploads`.
- Each upload writes to `inputs/uploads/<safe relative filename>`.
- Parent directories are created as needed.
- Existing files with the same relative path in the run workspace are replaced
  by the latest upload for that run.
- Materialization happens only for the request that includes the uploads.

If a request resumes an existing chat `RunState`, the same run workspace is
used. Uploaded files for the new request are written into that existing
workspace.

### Static DAG Flow

Static DAG artifact upload already supports directory-like artifact
materialization when multiple files or relative filenames are provided. The
frontend should stop discarding relative paths for folder uploads, and the
backend should keep validating uploaded filenames before materialization.

## Safety And Boundaries

All upload paths must fail closed:

- Reject absolute POSIX paths.
- Reject absolute Windows paths and drive-prefixed paths.
- Reject `..` segments after slash normalization.
- Reject empty upload filenames.
- Ensure every resolved destination remains within the run workspace.

Uploads should not bypass existing capability boundaries. Tools still run inside
the run workspace context, and file access continues to depend on selected
capabilities and review policy.

## Frontend Design

Introduce a shared upload helper for file and folder selection:

- File input supports `multiple`.
- Folder input uses browser directory selection support where available.
- File objects retain `webkitRelativePath` when folder uploads are selected.
- The helper produces a normalized display path and the form filename sent to
  the backend.

Smart workbench state should track pending uploads separately from run
artifacts:

- Pending uploads are client-side files waiting for the next message.
- Run artifacts are backend-listed files from `runState.workspace_path`.
- A successful request submission consumes the pending uploads for that
  request.
- Requests that fail before the backend accepts the upload should keep pending
  uploads available so the user can retry.

Static orchestration should reuse the helper but continue to update the DAG
artifact model before upload.

## Data Flow

1. User selects files or a folder.
2. Frontend normalizes display paths while preserving relative directory paths.
3. Smart workbench stores files as pending uploads, or static orchestration
   creates hidden uploaded-file artifacts.
4. User sends a smart workbench message or runs the static DAG.
5. Frontend sends files using multipart form data with relative filenames.
6. Backend validates upload filenames.
7. Backend materializes files into the run workspace.
8. Existing run artifact listing discovers the files.
9. Existing preview and download endpoints serve supported files.

## Testing

Backend tests:

- Smart workbench upload materializes a single file into `inputs/uploads`.
- Smart workbench folder upload preserves nested paths.
- Upload paths with `..`, absolute POSIX paths, and Windows drive paths are
  rejected.
- Uploads on a follow-up chat request reuse the existing run workspace.
- Static DAG folder upload preserves relative paths.

Frontend tests:

- Shared upload filename helper preserves `webkitRelativePath`.
- Static DAG upload no longer forces `preserveRelativePath: false` for folder
  uploads.
- Smart workbench pending uploads are sent with the next message and retained on
  request failure.

Verification commands:

```bash
uv run --extra dev pytest tests/test_api.py
npm --prefix web test
npm --prefix web run build
git diff --check
```

## Future Project Extension

Future project management can promote the smart workbench upload store from
session-scoped pending inputs to project-scoped files:

- Projects own a workspace path.
- Multiple sessions can share that path.
- The composer upload action can target either pending run input files or
  project files.
- Run workspaces can be created under or linked to a selected project workspace.

This design keeps the current implementation small while aligning path
contracts, upload validation, and UI behavior with that future direction.
