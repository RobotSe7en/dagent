# Migration Notes

dagent has released public SDK contracts. This page records user-facing changes
that may require action when upgrading.

## Current Release Line

The current package version is `0.5.2`.

## Unreleased

- No unreleased migration notes.

## 0.5.2

- Breaking change: `Runner(...)` and `Runner.from_config(...)` now default their
  workspace to `.dagent`, and each run records a workspace under
  `.dagent/runs/<run_id>` unless `workspace` or `workspace_root` is set
  explicitly.
- Breaking change: built-in file and shell tools now resolve relative paths from
  the current ToolAgent or DagAgent message run workspace, not from the runner
  workspace root. A `write_file(path="notes.txt", ...)` call in a normal agent
  run now writes under `.dagent/runs/<run_id>/notes.txt` instead of
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
  written as `shell(command="...", cwd=".")`. Replace saved `tool.run_command`
  capability ids and `run_command(...)` plan calls before upgrading. No legacy
  alias is registered.

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
