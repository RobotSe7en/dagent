# Capabilities

Capabilities are executable actions registered with a `Runner`. Agents and DAG
nodes do not execute functions directly; they call capability ids through the
runtime capability catalog.

## Capability Ids

| Source | Common id form |
| --- | --- |
| Python function tools | `tool.<name>` |
| MCP tools | `mcp.<server>.<tool>` |
| Built-in skill accessors | `skill.list`, `skill.view` |
| Memory accessors | `memory.write`, `memory.search` |
| Registered subagents | `agent.<name>` |

Capability ids are public behavior. Do not depend on legacy aliases that are not
documented here.

Raw `CapabilityDefinition.id` values must start with one of the supported kind
prefixes (`tool`, `mcp`, `agent`, `skill`, `memory`) and contain at least two
dotted segments. Every segment may contain only letters, numbers, and
underscores; leading or trailing whitespace is rejected. The table lists the
forms dagent uses by default, not a fixed segment count for every custom
capability.

## Built-in Tools

Every `Runner` registers a small default tool set. All path parameters are
checked against the node boundary before the handler runs. Tool-agent runs pause
for human review when a capability call would otherwise cross its boundary; an
approval applies only to that single capability call. DAG review approval is
broader: approving a DAG version authorizes its reviewed nodes to execute with
their shown boundaries. That authorization comes from the DAG review resume
flow; static DAGs and fast no-review DAG revisions still enforce node
boundaries and fail closed on boundary violations.

| Tool | Risk | Behavior |
| --- | --- | --- |
| `tool.read_file` | low | Read a UTF-8 text file. Optional `offset` (1-indexed) and `limit` page through large files; reads are capped at 2000 lines / 200 KB and oversized reads end with a `[TRUNCATED]` line that names the shown range. Binary files fail with a clear error. A full untruncated read returns the file content verbatim. |
| `tool.write_file` | medium | Write UTF-8 text to a file, creating parent directories. New files follow the process umask, replacement writes preserve the existing file mode, and replacement writes detach the target path from any hardlinked siblings. Returns the byte count written. |
| `tool.edit_file` | medium | Replace one exact occurrence of `old_string` with `new_string`. The match must be unique and byte-for-byte after UTF-8 decoding: zero matches and ambiguous matches fail with instructions to read the file and add surrounding context. Existing line endings and a UTF-8 BOM are preserved; the result includes a short unified diff. |
| `tool.list_files` | low | List files and directories under a path (directories end with `/`), up to `depth` levels (default 3). With `glob` (e.g. `*.py`) it lists matching files only. Output stops after 500 entries; the structured result value is the shown entry list, so DAG map nodes can fan out over it. |
| `tool.grep` | low | Search files with Python regular-expression syntax and an optional `glob` filename filter. Delegates to ripgrep with compatible flags when `rg` is on `PATH` (argv invocation, never a shell) and falls back to a pure-Python scan otherwise. Project ignore files are not applied; built-in heavy directory exclusions are applied in both backends. Output is `file:line:content`, capped at 200 matches. |
| `tool.shell` | high | Run a shell command in a bounded working directory with a 30s default timeout. Dangerous patterns are hard-blocked, the working directory must exist, explicit shell path arguments are checked against the boundary, and oversized output keeps the tail (200 lines / 100 KB) under a `[TRUNCATED]` header. A timeout terminates the command's process group, including children in pipelines, drains its output, and returns a terminal `timed out after ... seconds` error. |

Each capability has three names. `id` is the stable execution identity used in
scopes, traces, reviews, and DAG invocation payloads. `name` is the LLM-visible
function name used by provider tool calls. Typed dynamic DAG plans reference
the stable `id` directly. `display_name` is UI-only text. When `name` is
omitted, dagent defaults it to the capability id with dots replaced by
underscores; when `display_name` is omitted, it defaults to `name`.

`tool_read_file` output carries no line-number prefixes, so text copied from a
read result can be passed to `tool_edit_file` as `old_string` unchanged. The
intended editing flow is: read the file, copy the exact text to change, then
call `tool_edit_file` with enough surrounding context to make the match unique.

## Sandbox Execution

`execution="sandbox"` currently supports only the built-in tool capabilities
listed above. Their boundary checks run on the host first, and the checked tool
call is then routed through the active sandbox session.

Python function tools registered with `@dagent.tool` or
`Runner.register_capability(...)`, MCP tools, skill capabilities, memory
capabilities, agent capabilities, DAGs, `DAGSpec`, and `DagAgent` are not
sandbox-executable yet. They fail closed under `execution="sandbox"` instead of
falling back to host execution. Use `execution="local"` for those capabilities.

`Runner.test_capability(..., execution="sandbox")` uses the runner workspace as
the sandbox workspace. The runner workspace is an explicit
`Runner(workspace=...)` input; files already present there are visible to
supported built-in tools.

## Python Function Tools

Decorate Python functions with `@dagent.tool`. Parameter annotations produce
tool input JSON schema; return annotations produce output schema. The Python
function name still determines the capability id: `search` registers
`tool.search`. Pass `name=` to choose the provider tool-call function name, and
`display_name=` to choose UI text. The decorator does not accept `id=`.

```python
from pydantic import BaseModel

import dagent


class SearchResult(BaseModel):
    title: str
    url: str


@dagent.tool
def search(q: str) -> SearchResult:
    return SearchResult(title=f"found:{q}", url="https://example.test")
```

Register tools at construction or later:

```python
runner = dagent.Runner(workspace="agent-workspace", runtime_directory=".runtime", provider=provider, capabilities=[search])
runner.add_tool(search)
```

Use `runner.add_tools([...])` for an atomic batch. Runtime managers that own a
configured set of Python function tools can use
`runner.reload_tools(groups, replace_ids=...)` to remove the previous owned ids,
register current groups independently, and collect group or registered-agent
errors without treating missing old ids as a user deletion. `replace_ids` may
only name non-built-in `tool.*` capabilities; MCP tools, agent capabilities, and
built-in tools must be managed through their own lifecycle APIs.

Hosts that persist user-configured Python tool sources can load explicit
`UserPythonToolConfig` entries through the SDK instead of importing files
themselves:

```python
from pathlib import Path


result = runner.reload_python_tool_sources(
    configs,
    user_config_dir=Path("~/.dagent").expanduser(),
    managed_root=Path("~/.dagent/python-tools").expanduser(),
    replace_ids=previous_python_tool_ids,
)

print(result.capability_ids_by_source)
print(result.errors)
```

For preview and validation flows, import
`discover_python_tool_names`, `load_python_tool_sources`, or
`read_python_tool_source` from `dagent.capabilities.python_tools`. These helpers
load `path`, `managed`, and `module` sources; they load only explicit `names`,
validate that each export is a `CapabilityBinding` produced by `@dagent.tool`,
and report per-source errors without implicitly scanning directories. Source
reading and decorator-name discovery are file-based and do not read installed
modules.

Agents declare what they can use:

```python
agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["tool.search"],
)
```

Rename the Python function if you need a different public capability id.

## Structured Results

Plain `str`, `dict`, `list`, numbers, booleans, tuples, bytes, and Pydantic
models are converted into `CapabilityResult.content` and `CapabilityResult.value`.
Static DAG node output references read from `value` by default.

If a tool returns `CapabilityResult` directly, completed results with no explicit
`value` use `content` as the value.

## Tool Context and Boundaries

Tools that need the run workspace or callbacks can opt into runtime context:

```python
from pathlib import Path

import dagent


@dagent.tool(risk="medium", supports_context=True)
def write_note(path: str, content: str, *, context, callbacks=None) -> str:
    run_workspace = Path(context.workspace_path).resolve()
    resolved = Path(path).resolve()
    resolved.relative_to(run_workspace)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"wrote:{path}"
```

DAG nodes can pass boundaries for side-effecting work:

```python
report = dag.artifact("report", "outputs/report.md")

write_node = dagent.Node(
    "write_report",
    target=write_note,
    inputs={"path": report.absolute_path, "content": search_node.output},
    artifact_outputs=[report],
    boundary=dagent.Boundary(
        allowed_paths=[report.absolute_path.as_expr()],
    ),
)
```

Boundaries declare the paths a node may read or write. Boundary values can be
literal strings or structured value references.

## Capability Policies

`CapabilityPolicy` records risk and execution requirements:

```python
policy = dagent.CapabilityPolicy(
    risk="medium",
    requires_review=True,
    network=False,
    secrets=[],
)
```

Review settings on agents and runs determine when medium/high-risk work pauses
for approval. Boundary review is independent of risk review: a tool-agent call
that tries to read or write outside its boundary pauses with
`payload.reason == "boundary_violation"`. Approving that review executes the
same call and authorizes the reported `payload.boundary_paths` for later tool
calls in the same run. Other paths still require review, and the authorization
does not carry into another run. Rejecting it feeds a denial message back to the
agent. Hard-blocked shell patterns, such as destructive system commands, are
not reviewable.

## MCP Tools

MCP stdio and Streamable HTTP server tools become ordinary
`mcp.<server>.<tool>` capabilities after server registration:

The `<server>` and `<tool>` segments are dagent public keys. Raw MCP server and
tool names are preserved in capability `config`, and unsafe raw names are
canonicalized with a stable short hash so different external names do not
collide after normalization. Inspect the definitions returned by
`runner.add_mcp_server(...)` or `/capabilities` instead of guessing ids for
third-party tools.

```python
runner.add_mcp_server(
    "fs",
    {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
    },
)

agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["mcp.fs.read_file"],
)
```

For a remote Streamable HTTP server, register an explicit HTTP transport:

```python
runner.add_mcp_server(
    "remote_docs",
    {
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
    },
)
```

Install the MCP extra before registering MCP servers:

```bash
pip install "dagent-ai[mcp]"
```

See [Runner and Configuration](runner-and-configuration.md) for dynamic MCP
registration and replacement.

After registration, `runner.mcp_server_snapshot(name)` and
`runner.list_mcp_server_snapshots()` return read-only MCP identity snapshots
with capability ids, raw server/tool names, and public capability definitions.
For bulk host reloads, `runner.reload_mcp_servers_with_snapshots(...)` uses the
same batch reload semantics as `runner.reload_mcp_servers(...)` while returning
`MCPServerRegistrationResult` with successful snapshots and per-server errors.
Use these SDK results when
persisting discovered tools instead of rebuilding `mcp.<server>.<tool>` ids
outside the SDK.

Hosts that already trust a saved `MCPServerSnapshot` can pass it back with
`lazy_connect=True`:

With `lazy_connect=True`, every enabled server requires an SDK-produced
snapshot; missing or non-canonical snapshots fail before catalog registration.

```python
runner.add_mcp_server(
    "remote_docs",
    config,
    snapshot=snapshot,
    lazy_connect=True,
)
```

This registers the snapshot's capability definitions without connecting to the
server immediately. The SDK connects that MCP server on the first tool call.
The snapshot is metadata for registration and validation; executable behavior
still comes from the configured MCP server.

## Direct Capability Tests

Use `Runner.test_capability(...)` to execute one capability for inspection:

```python
result = await runner.test_capability("tool.search", {"q": "dagent"})
print(result.status)
print(result.value)
```
