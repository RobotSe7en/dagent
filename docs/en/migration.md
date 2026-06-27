# Migration Notes

dagent has released public SDK contracts. This page records user-facing changes
that may require action when upgrading.

## Current Release Line

The current package version is `0.6.0`.

## Unreleased

- No unreleased changes.

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

- Capability definitions now separate stable ids from call names. `id` remains
  the execution identity; `name` is the LLM/PlanSpec function name; and
  `display_name` is UI-only text.
- Runner capability registration now keeps registered subagent runtime scopes
  aligned as tools, MCP servers, raw capabilities, and skill visibility change.
- Local API agent preset payloads now use public `ToolAgent` field names and
  enforce the same leaf-subagent constraints as the SDK.

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
