# Capabilities

Capabilities are executable actions registered with a `Runner`. Agents and DAG
nodes do not execute functions directly; they call capability ids through the
runtime capability catalog.

## Capability Ids

| Source | Id format |
| --- | --- |
| Python function tools | `tool.<name>` |
| MCP stdio tools | `mcp.<server>.<tool>` |
| Built-in skill accessors | `skill.list`, `skill.view` |

Capability ids are public behavior. Do not depend on legacy aliases that are not
documented here.

## Built-in Tools

Every `Runner` registers a small default tool set. All path parameters are
checked against the node boundary before the handler runs.

| Tool | Risk | Behavior |
| --- | --- | --- |
| `tool.read_file` | low | Read a UTF-8 text file. Optional `offset` (1-indexed) and `limit` page through large files; reads are capped at 2000 lines / 200 KB and oversized reads end with a `[TRUNCATED]` line that names the shown range. Binary files fail with a clear error. A full untruncated read returns the file content verbatim. |
| `tool.write_file` | medium | Write UTF-8 text to a file atomically (temp file + rename), creating parent directories. Returns the byte count written. |
| `tool.edit_file` | medium | Replace one exact occurrence of `old_string` with `new_string`. The match must be unique: zero matches and ambiguous matches fail with instructions to read the file and add surrounding context. CRLF line endings and a UTF-8 BOM are preserved; the result includes a short unified diff. |
| `tool.grep` | low | Search files for a regular expression with an optional `glob` filename filter. Delegates to ripgrep when `rg` is on `PATH` (argv invocation, never a shell) and falls back to a pure-Python scan otherwise. Output is `file:line:content`, capped at 200 matches. |
| `tool.run_command` | high | Run a shell command in a bounded working directory with a 30s default timeout. Dangerous patterns are hard-blocked, the working directory must exist, and oversized output keeps the tail (200 lines / 100 KB) under a `[TRUNCATED]` header. |

`read_file` output carries no line-number prefixes, so text copied from a read
result can be passed to `edit_file` as `old_string` unchanged. The intended
editing flow is: read the file, copy the exact text to change, then call
`edit_file` with enough surrounding context to make the match unique.

## Python Function Tools

Decorate Python functions with `@dagent.tool`. Parameter annotations produce
tool input JSON schema; return annotations produce output schema.

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
runner = dagent.Runner(provider=provider, capabilities=[search])
runner.add_tool(search)
```

Agents declare what they can use:

```python
agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["tool.search"],
)
```

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
    resolved = Path(context.workspace_path) / path
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
    inputs={"path": report.path, "content": search_node.output},
    artifact_outputs=[report],
    boundary=dagent.Boundary(
        mode="write_limited",
        allowed_paths=[report.path.as_expr()],
    ),
)
```

Boundary modes are `read_only`, `write_limited`, and `full`. Boundary values can
be literal strings or structured value references.

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
for approval.

## MCP Tools

MCP stdio server tools become ordinary `mcp.<server>.<tool>` capabilities after
server registration:

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

Install the MCP extra before registering MCP servers:

```bash
pip install "dagent-ai[mcp]"
```

See [Runner and Configuration](runner-and-configuration.md) for dynamic MCP
registration and replacement.

## Direct Capability Tests

Use `Runner.test_capability(...)` to execute one capability for inspection:

```python
result = await runner.test_capability("tool.search", {"q": "dagent"})
print(result.status)
print(result.value)
```
