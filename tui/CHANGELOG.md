# Changelog

## 0.1.0 — Unreleased

### Added

- API-backed Textual workbench with standalone and project conversation history.
- Streaming answer, reasoning, capability, validation, DAG, and trace display.
- Persisted human-review approval/rejection with feedback and checkpoint resume.
- Active-run cancellation, last-prompt retry, and automatic creation of chat or
  dynamic-DAG conversations.
- Dedicated package, lockfile, command entrypoint, and headless interaction tests.

### Behavior and compatibility

- Requires Python 3.11 or newer and an existing dagent FastAPI host.
- Uses only existing public HTTP/SSE request shapes; it does not import or modify
  the dagent SDK runtime.
- Dynamic DAG conversations get an orchestration session so their visible message
  timeline remains available after restart.

### Known limitations

- New conversations are standalone; existing project conversations can be opened
  and continued, but creating a new project conversation is not in this release.
- DAGs use a read-only terminal summary. Graph editing, uploads, artifact previews,
  and provider/MCP/skill administration remain in the WebUI.

### Verification

- Headless TUI and client tests cover history, streaming, review resume, retry,
  cancellation, and dynamic-DAG conversation creation.
- Source and wheel builds are verified from the standalone `tui/` project.
