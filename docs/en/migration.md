# Migration Notes

dagent has released public SDK contracts. This page records user-facing changes
that may require action when upgrading.

## Current Release Line

The current package version is `0.6.8`.

## Unreleased

### Added

- `Runner.derive(...)` can inherit local `CapabilityBinding` tool registrations
  from the base runner with `inherit_local_tools=True`, and can skip caller-owned
  tool ids with `exclude_local_tool_ids`.
- `RuntimeRunSpec`, `RuntimeFrame`, and related `dagent.schemas` contracts define
  the process-boundary payloads consumed by `python -m dagent.worker`.
- `Runner.run(...)` and `Runner.stream(...)` accept a host-provided `run_id` for
  new runs; duplicate explicit run ids are rejected unless the caller supplies
  matching `RunState` to continue an existing run.
- MCP servers can be registered from trusted snapshots with lazy MCP connection
  by passing `snapshot=...` and `lazy_connect=True`; lazy registration requires a
  snapshot and still applies the current server config filters and policy.
- `Runner.validate_capability_refs(...)` validates saved capability ids without
  registering tools or agents and returns machine-readable `ValidationResult`
  issues.

### Changed

- Runtime `bye` frames now distinguish worker process failure from failed dagent
  runs, and resume runtime specs must include the serialized `RunState` they
  continue.

## 0.6.8

### Added

- `dagent.capabilities.python_tools` now provides SDK-owned helpers for loading
  configured `@dagent.tool` Python sources from explicit path, managed, or
  module entries.
- `Runner.reload_python_tool_sources(...)` loads configured Python tool sources
  and returns a stable registration result without exposing executable bindings.
- `Runner.derive(...)` creates an independent runner with explicit provider,
  workspace, MCP server, Python tool, agent, profile, sandbox, and validation
  overlays.
- `Runner.mcp_server_snapshot(...)`,
  `Runner.list_mcp_server_snapshots()`,
  `Runner.reload_mcp_servers_with_snapshots(...)`, and
  `Runner.catalog_view(...)` expose read-only runner-owned capability and MCP
  registration views without handler or catalog internals.

### Changed

- The local API/WebUI backend now uses SDK-owned Python tool loading helpers
  instead of maintaining a separate loader implementation.
- Architecture guidance now explicitly discourages legacy-test compatibility
  code, duplicate implementations, broad fallback behavior, and enterprise
  concerns in the installable SDK.

### Fixed

- The architecture boundary test no longer fails on obsolete `tool_review`
  wording in persistence test names.

### Breaking Changes

- None for the public Python SDK.

### Migration Steps

- SDK users do not need to change existing code.
- Hosts that previously loaded configured Python tools or inspected MCP/catalog
  internals should migrate to the new `Runner` methods and keep RBAC,
  redaction, persistence, and effective-configuration composition in the host
  layer.

### Verification

- `uv run --extra dev pytest`
- `uv build`
- `git diff --check`

### Known Limitations

- `Runner.catalog_view(...)` is a runtime registration view, not a user-facing
  authorization surface. Hosts remain responsible for RBAC, redaction, and
  policy filtering before returning catalog data to users.

## 0.6.7

### Added

- None.

### Changed

- Tool-agent and dynamic DAG LLM calls now retry transient provider failures
  and request timeouts with increasing waits before surfacing the existing
  failure path.
- Default LLM retry waits remain `1`, `2`, `5`, `10`, and `30` seconds.

### Fixed

- Permanent LLM provider failures, such as invalid request payloads or
  non-retryable client errors, are no longer retried.
- Streaming LLM calls no longer retry after response tokens have been emitted,
  avoiding duplicated partial stream output.

### Breaking Changes

- None for the public Python SDK.

### Migration Steps

- No SDK migration action is required.

### Verification

- `uv run --extra dev pytest`
- `uv build`
- `git diff --check`

### Known Limitations

- LLM retry classification is intentionally conservative. Custom providers
  should surface timeouts, connection errors, retryable status codes, or
  provider-specific transient exception names for automatic retries.

## 0.6.6

### Added

- The local WebUI can edit Office documents through ONLYOFFICE for project
  files and run artifacts when the corresponding edit settings are enabled.
- Dynamic and static orchestration workspaces now persist run history, support
  historical run inspection, and can delete stored run history entries.
- Chat and dynamic DAG conversations now persist visible message timelines so
  conversations can be hydrated directly after API restarts.
- `examples/local_test_mcp.py` provides a local stdio MCP server for
  registration and tool-call timeout diagnostics.

### Changed

- MCP tool calls now default to a `300` second timeout, and the local WebUI MCP
  form exposes the tool timeout setting.
- Project files and run artifact metadata now include file `version` values so
  Office previews can refresh when backing files change.

### Fixed

- MCP connection and tool-call timeouts now report explicit timeout messages
  instead of empty error text.
- Orchestration run history rows, hydration, run summaries, and workspace
  separation have been tightened across dynamic, static, standalone, and
  project-scoped flows.
- Interrupted chat and persisted dynamic DAG history hydration handle completed
  traces and visible turns more consistently.

### Breaking Changes

- None for the public Python SDK.

### Migration Steps

- No SDK migration action is required.
- MCP users who rely on the previous shorter implicit tool-call cutoff can set
  `tool_timeout` explicitly in their MCP server config.
- Existing local WebUI SQLite storage may be recreated if it was produced by an
  incompatible pre-release schema.

### Verification

- `uv run --extra dev pytest`
- `npm --prefix web test`
- `npm --prefix web run build`

### Known Limitations

- The local storage backend is SQLite plus local filesystem workspaces. Cloud
  or multi-worker deployments still need the planned Postgres, object-storage,
  and worker execution backends.

## 0.6.5

### Added

- The local API/WebUI project file browser can request a recursive project file
  tree with nested preview and download metadata while skipping unsafe workspace
  escapes.
- The WebUI now summarizes completed chat process timelines so final answers
  stay visible while reasoning, validation, and capability activity remain
  inspectable.

### Changed

- MCP server registration and tool calls now share explicit default timeouts:
  `connect_timeout` defaults to `60` seconds and `tool_timeout` defaults to `90`
  seconds for stdio and Streamable HTTP servers.
- The WebUI project workspace, static DAG workspace, chat drafts, run settings,
  and completed-run traces have been refined for denser navigation and steadier
  layouts.

### Fixed

- Persisted chat trace hydration now replays stored stream envelopes through the
  same dispatcher as live streams, preventing missing or inconsistent completed
  traces after refresh.
- Local API workspace file URIs now use platform-aware file URI handling, fixing
  Windows workspace paths.
- Project file tree listing skips symlink workspace escapes and avoids directory
  cycles.

### Breaking Changes

- None for the public Python SDK.

### Migration Steps

- No SDK migration action is required.
- MCP users who rely on the previous shorter implicit tool-call cutoff can set
  `tool_timeout` explicitly in their MCP server config.

### Verification

- `uv run --extra dev pytest`
- `npm --prefix web test`
- `npm --prefix web run build`

### Known Limitations

- The local storage backend is SQLite plus local filesystem workspaces. Cloud
  or multi-worker deployments still need the planned Postgres, object-storage,
  and worker execution backends.

## 0.6.4

### Added

- The local API/WebUI now persists dynamic and static DAG orchestration sessions,
  including draft DAG state, selected-node UI state, saved static DAG links, run
  event history, and run state snapshots.
- Saved static DAGs now preserve saved-record metadata, revisions, project
  ownership, editor layout, and persisted artifact uploads across API process
  restarts.
- Static orchestration run timelines can be restored from durable run events
  after a run completes.

### Changed

- The local API/WebUI store now isolates chat, dynamic DAG, and static DAG
  conversations by kind; ordinary chat streams reject orchestration
  conversations.
- The static orchestration UI keeps saved DAG display names and visible revision
  state stable across name edits, refreshes, and run completion.
- The WebUI now enters an empty chat when opening the chat workspace instead of
  automatically selecting an existing conversation. Artifact panels can still
  expand when artifacts exist without auto-selecting a preview file.
- The non-public legacy `/dags/{dag_id}/run/stream` local API route was removed.
  Persisted static DAG runs use `/saved-dags/{dag_id}/run/stream`.

### Fixed

- The dynamic orchestration page now keeps its history and run workspaces
  separate from projects. Project-scoped DAG conversations remain in the smart
  workbench project flow.
- Static orchestration run events and final timelines no longer disappear after
  run completion.
- Static orchestration hydration no longer replays saved DAG state over the
  current editor and completed run results after a save or conversation refresh.
- Concurrent static run attempts no longer silently lose orchestration session
  creation when the same conversation session is created by another request.

### Breaking Changes

- None for the public Python SDK.

### Migration Steps

- No SDK migration action is required.
- Incompatible pre-release local SQLite API databases are recreated instead of
  migrated. This does not affect the public Python SDK.

### Verification

- `uv run --extra dev pytest`
- `npm --prefix web test`
- `npm --prefix web run build`

### Known Limitations

- The local storage backend is SQLite plus local filesystem workspaces. Cloud
  or multi-worker deployments still need the planned Postgres, object-storage,
  and worker execution backends.

## 0.6.3

### Added

- The local API now persists projects, standalone conversations, project
  conversations, run state snapshots, run event history, and review records in
  the API storage layer without changing the public SDK.
- The WebUI now separates standalone conversations from projects. Projects can
  be expanded to show their conversations, and each project exposes a detail
  workspace with file management, directory browsing, upload, rename, delete,
  download, preview, and document-preview configuration support.
- Persisted chat streams can resume from stored `RunState` snapshots, including
  pending reviews, artifact manifests, traces, and project conversation state
  after an API process restart.
- Standalone conversations now use a durable workspace under
  `.dagent/projects/_conversations/<conversation_id>/workspace`; project
  conversations share the project workspace under
  `.dagent/projects/<project_id>/workspace`.

### Changed

- Persisted conversations are single-writer per conversation. The API uses
  conversation locks with leases so another stream cannot drive the same
  conversation concurrently.
- Deleting a standalone conversation removes its database rows and its
  conversation root directory. Deleting a project conversation removes the
  conversation and run records while preserving the shared project workspace.
- Project deletion removes the project database records and the local project
  root directory after checking that no project conversation is active.
- The system settings label for OnlyOffice has been renamed to Document
  Configuration in the WebUI.

### Breaking Changes

- None for the public Python SDK.

### Migration Steps

- No SDK migration action is required.
- Existing local one-off runs and static/dynamic orchestration runs remain in
  their previous run workspaces. New persisted chat conversations use the
  `.dagent/projects/...` workspace layout.
- If you tested an unreleased 0.6.3 development branch before this release, you
  can remove any stale empty directories under `.dagent/projects/_conversations`
  that were created before standalone conversation root cleanup was added.

### Verification

- `uv run --extra dev pytest`
- `npm --prefix web test`
- `npm --prefix web run build`

### Known Limitations

- Static DAG runs and the dynamic orchestration workspace still use the existing
  run workspace model unless a workspace root is explicitly provided. They have
  not yet been folded into project/conversation persistence.
- The local storage backend is SQLite plus local filesystem workspaces. Cloud
  or multi-worker deployments still need the planned Postgres, object-storage,
  and worker execution backends.

## 0.6.2

### Added

- The WebUI now supports browser previews for additional artifact types,
  including Office documents and PPTX artifacts.
- The local API and WebUI expose OnlyOffice configuration for richer document
  preview workflows when an OnlyOffice server is configured.
- OnlyOffice-backed DOCX, XLSX, and PPTX previews can now be switched into edit
  mode independently for project files and run artifacts. Edit mode disables
  autosave and only writes back when the user clicks Save.
- Run artifact and project file metadata now include a `version` field derived
  from file size and nanosecond mtime for precise preview cache invalidation.
- View-only ONLYOFFICE previews keep a small browser-side cache of recently
  opened editor instances to speed up switching between unchanged documents.
- Chat workbench uploads can now be attached to a message and materialized into
  the run workspace so agents can inspect user-provided files.
- Static DAG output binding and schema-argument editing support have been
  expanded in the WebUI.
- Tools and MCP resources now use richer tree-style navigation in the WebUI.

### Changed

- Artifact preview chrome, artifact-tree interactions, and collapsed artifact
  rail behavior have been refined for denser workspace use.
- Workbench upload handling is stricter about filenames and workspace
  boundaries.
- OnlyOffice preview URLs now use signed file tokens that also carry whether the
  current preview session may save edits. The callback handler only overwrites
  files for user-triggered force-save callbacks.

### Breaking Changes

- None.

### Migration Steps

- No migration action is required for this patch release.
- To use Office document previews or editing, configure an OnlyOffice document
  server in the WebUI system settings. Editing remains off until the project-file
  or run-artifact edit switch is enabled.

### Verification

- `uv run --extra dev pytest`
- `source ~/.nvm/nvm.sh && npm --prefix web test`
- `source ~/.nvm/nvm.sh && npm --prefix web run build`

### Known Limitations

- Office previews require an external OnlyOffice document server.
- Editing run artifacts mutates files in the run workspace but does not rewrite
  stored trace history.
- Workbench uploads are materialized into the local run workspace; they are not
  persisted across ephemeral container storage without a persistent volume.

## 0.6.1

### Added

- The local WebUI now persists user-managed model providers, the active model,
  user MCP servers, and explicitly imported Python tool sources through the
  user config file.
- The WebUI can import Python tools from local paths or uploaded `.py` files,
  manage their enabled state, validate them, reload them, and remove uploaded
  managed files.
- The Python tool import dialog now discovers top-level functions decorated
  with `@tool` or `@dagent.tool` and auto-fills the function-name list while
  keeping the list editable.
- The WebUI exposes MCP servers in the MCP service view but no longer lists MCP
  capabilities in the generic tools view.

### Changed

- Capability definitions now separate stable ids from call names. `id` remains
  the execution identity; `name` is the LLM/PlanSpec function name; and
  `display_name` is UI-only text.
- Runner.add_tools is now atomic: if any binding in a batch cannot be
  registered, the runner leaves the catalog unchanged. Re-registering an
  identical existing binding remains idempotent.
- Local WebUI Python tool entries using `source: "module"` no longer reload
  modules that are already present in `sys.modules`. Use `path` or uploaded
  `managed` sources when you need reload-style development behavior.
- `/python-tools/reload` now reloads only imported Python-tool capabilities. It
  no longer restarts the whole runner or reconnects unrelated MCP servers;
  presets that reference removed Python tools are reported as agent errors.

### Breaking Changes

- `@dagent.tool` still does not accept `id=`. Python function tools always
  derive their capability id from the function name as `tool.<function_name>`.
  `name=` is accepted again, but it controls only the LLM/PlanSpec function
  name; it does not change the capability id.
- `CapabilityDefinition.name` and `CapabilityDefinition.display_name` are
  public fields. If omitted, `name` defaults to the capability id with dots
  replaced by underscores, and `display_name` defaults to `name`.
- Raw `CapabilityDefinition.id` values must use dotted capability ids beginning
  with `tool`, `agent`, `mcp`, `skill`, or `memory`. Each segment may contain
  only letters, numbers, and underscores; at least two segments are required.
- LLM-visible PlanSpec and tool-call function names now use
  `CapabilityDefinition.name`. Update saved dynamic DAG PlanSpec text and
  deterministic provider fixtures if you set custom names.

### Migration Steps

- If you use custom capability `name` values, update saved dynamic DAG PlanSpec
  text and deterministic provider fixtures to call those names.
- Inspect raw capability definitions and saved allowlists for dotted capability
  ids that do not start with `tool`, `agent`, `mcp`, `skill`, or `memory`.
- Rename WebUI-managed profiles, agent presets, and user MCP server keys that
  contain dashes or other characters outside letters, numbers, and underscores.
- Use `path` or uploaded `managed` Python tool sources for reload-style local
  development; `module` sources reuse already imported modules.

### Verification

- `uv run --extra dev pytest tests/test_api.py tests/test_python_tool_imports.py -q`
- `source ~/.nvm/nvm.sh && npm --prefix web test`
- `source ~/.nvm/nvm.sh && npm --prefix web run build`

### Known Limitations

- Python tool auto-discovery recognizes literal `@tool` and `@dagent.tool`
  decorators. If a source imports either name through an alias, enter the
  function names manually.
- Invalid legacy WebUI agent preset files are reported as errors and are not
  migrated automatically.

## 0.6.0

### Added

- Single-level subagent delegation is now part of the public SDK.
  `Runner.add_agent(...)` and `Runner.add_agents(...)` register leaf
  `ToolAgent` configurations as `agent.<name>` capabilities.
- Top-level `ToolAgent`, `AutoAgent`, and `DagAgent` runs can expose selected
  subagents through `agents=["agent.<name>"]`, direct `ToolAgent` objects, or
  `agents="registered"` for all agents registered on the runner.
- Static `Dag` nodes can target `ToolAgent` objects in Python. Managed API/WebUI
  agent presets are exposed as `agent.<name>` capabilities, with their selected
  tools, MCP capabilities, and skills mapped onto public `ToolAgent` fields.
- The new `examples/agent_delegation.py` example demonstrates registering a leaf
  subagent and exposing it to a top-level run.

### Changed

- LLM-visible function names are now consistently derived from stable
  capability ids by replacing dots with underscores. For example,
  `tool.search` becomes `tool_search(...)`, and `agent.helper` becomes
  `agent_helper(...)`.
- Runner capability registration now keeps registered subagent runtime scopes
  aligned as tools, MCP servers, raw capabilities, and skill visibility change.
- Local API agent preset payloads now use public `ToolAgent` field names and
  enforce the same leaf-subagent constraints as the SDK.

### Breaking Changes

- `@dagent.tool` no longer accepts `id=` or `name=`.
  Python function tools always derive their capability id from the function
  name as `tool.<function_name>`. Rename the function or wrap the implementation
  with a differently named function when changing the public id.
- `CapabilityDefinition.name` has been removed. Raw capability definitions now
  carry only `id` as their stable public identifier; LLM-visible function names
  are derived from that id.
- Raw `CapabilityDefinition.id` values must use supported dotted capability id
  forms: `tool.<name>`, `agent.<name>`, `mcp.<server>.<tool>`, `skill.<name>`,
  or `memory.<name>`. Each segment may contain only letters, numbers, and
  underscores; leading or trailing whitespace is rejected.
- LLM-visible PlanSpec and tool-call function names are now derived from
  capability ids by replacing dots with underscores. For example, use
  `tool_search(...)`, `tool_shell(...)`, and `agent_helper(...)` instead of
  short names such as `search(...)`, `shell(...)`, or `helper(...)`. Update
  saved dynamic DAG PlanSpec text and deterministic provider fixtures
  accordingly.
- MCP capability ids now use stable canonical keys for raw MCP server or tool
  names that are not already valid id segments. For example, `mock-server` no
  longer maps to `mock_server`; inspect registered capability definitions and
  update saved capability allowlists or DAG specs.
- Local API managed profile and agent preset names may contain only letters,
  numbers, and underscores. Replace dashes with underscores before creating new
  managed profiles or agent presets.
- Local API MCP server names are strict workspace keys and may contain only
  letters, numbers, and underscores. This does not restrict third-party MCP tool
  names, which are preserved in capability config.
- Local API agent preset JSON now uses `ToolAgent` field names. Replace
  `capability_ids` with `capabilities`; registered presets must keep `agents`
  empty and `review` set to `"fast"`. Old preset files are reported as errors
  and are not migrated automatically.

## 0.5.2

- Breaking change: `Runner(...)` and `Runner.from_config(...)` now default their
  workspace to `.dagent`, and each run records a workspace under
  `.dagent/runs/<run_id>` unless `workspace` or `workspace_root` is set
  explicitly.
- Breaking change: built-in file and shell tools now resolve relative paths from
  the current ToolAgent or DagAgent message run workspace, not from the runner
  workspace root. A `tool_write_file(path="notes.txt", ...)` call in a normal
  agent run now writes under `.dagent/runs/<run_id>/notes.txt` instead of
  `.dagent/notes.txt`. Code that expects files directly under the runner
  workspace should pass absolute paths or move shared inputs into each run
  workspace. Static DAG artifact paths still use their documented artifact
  mapping; pass `artifact.path` for built-in path-aware tools.
- Breaking change: `Boundary` now declares only `allowed_paths`. Remove
  `mode=` and `allowed_commands=` from SDK code, saved DAG specs, and API
  payloads. Shell command safety checks are enforced by the built-in shell tool
  rather than by per-node `allowed_commands`.
- MCP configs now support Streamable HTTP servers through explicit
  `transport: "http"`, `url`, and optional `headers`. Header values expand
  `${ENV_NAME}` at connection time. The optional MCP extra now requires
  `mcp>=1.27.1,<2`.

## 0.5.1

- No migration action is required for this patch release. It adds WebUI model
  provider management and API key redaction improvements without changing the
  documented public SDK contracts.

## 0.5.0

- Sandbox execution now fails closed for unsupported targets and capabilities.
  Only built-in tool capabilities execute inside `execution="sandbox"`. Python
  function tools, raw registered capabilities, MCP, skills, memory, agents,
  DAGs, `DAGSpec`, and `DagAgent` must use `execution="local"` until sandbox
  support is added for them; they no longer fall back to host execution when a
  sandbox run is active.

## 0.4.2

- The built-in shell command capability is now `tool.shell`, with DAG DSL calls
  written as `tool_shell(command="...", cwd=".")`. Replace saved
  `tool.run_command` capability ids and `run_command(...)` plan calls before
  upgrading. No legacy alias is registered.

## Public Surface Expectations

Treat the following as released behavior:

- package install name: `dagent-ai`
- Python import name: `dagent`
- public exports listed in [Python SDK Reference Map](python-sdk.md)
- Python tool capability ids using `tool.<name>`
- MCP capability ids using `mcp.<server>.<tool>`
- explicit `Runner(...)` inputs
- config-file loading through `Runner.from_config(...)`
- static DAG explicit dependency requirements
- review-safe continuation through `Runner.resume(...)`

## Capability Ids

Python function tools use `tool.<name>`. Do not rely on old or internal
capability id prefixes as compatibility aliases.

## Runner Configuration

`Runner(...)` uses explicit SDK inputs and does not implicitly read
`config.yaml`. Use `Runner.from_config(...)` when loading provider settings, MCP
servers, validation, or profile directories from a config file.

## Profiles

Built-in profiles are packaged Markdown resources under
`dagent/resources/profiles/<name>.md`. User profile directories must be passed
explicitly through `profile_root`.

## Static DAG Dataflow

Static DAGs require explicit dependencies. A value reference such as
`node.output.title` does not create an edge. Add the dependency with
`dag.add_edge(...)`.

## Future Entries

When a future release changes documented public behavior, add:

- affected version
- old behavior
- new behavior
- migration steps
- related examples or docs
