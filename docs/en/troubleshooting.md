# Troubleshooting

This page lists common setup and runtime issues for released dagent users.

## Import Fails After Install

Install the package as `dagent-ai`, but import it as `dagent`:

```bash
pip install dagent-ai
```

```python
import dagent
```

Check that your Python version is 3.11 or newer.

## `pip` Is Unavailable in a Repository Checkout

The local API and WebUI use the `dev` extra. Create or repair the project venv
with:

```bash
uv sync --extra dev
```

This installs the project's `pip` dependency. Do not rely on an unrelated
system `pip`; shell-enabled agents run with the project's virtual environment.

## Provider Authentication Fails

Make sure the environment variable passed to `api_key_env` exists in the same
process that starts your app:

```bash
export OPENAI_API_KEY="..."
```

```python
provider = dagent.Provider(
    base_url="https://api.openai.com/v1",
    model="your-model",
    api_key_env="OPENAI_API_KEY",
)
```

Use provider-specific `extra_request_args` or `extra_body` only when the target
endpoint documents those fields.

## `Runner.from_config(...)` Cannot Find Config

If no path is passed, config resolution uses:

1. `DAGENT_CONFIG`
2. `./config.yaml`

Pass the path explicitly when running from a different working directory:

```python
runner = dagent.Runner.from_config(
    "/path/to/config.yaml",
    workspace="agent-workspace",
    runtime_directory=".runtime",
)
```

## MCP Registration Fails

Install the MCP extra:

```bash
pip install "dagent-ai[mcp]"
```

Then confirm the configured stdio server command works outside dagent, or that
the configured Streamable HTTP `url` and `headers` work against the remote MCP
server. MCP registration is rolled back if the server cannot connect or a
discovered tool cannot register.

If startup is slow, increase `connect_timeout`; if a tool starts but fails while
running, increase `tool_timeout`. Timeout failures include explicit messages
such as `timed out after 60 seconds` or `MCP tool 'search' on server 'docs'
timed out after 300 seconds`.

## Unknown Capability

List registered capabilities:

```python
for definition in runner.list_capabilities():
    print(definition.id, definition.enabled)
```

Common id formats:

- Python tools: `tool.<name>`
- MCP tools: `mcp.<server>.<tool>`
- Skill accessors: `skill.list`, `skill.view`

## Agent Cannot See a Tool

Agents use their `capabilities` field as an allowlist. Passing an explicit list
narrows what the agent can call:

```python
agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["tool.search"],
)
```

If you pass a `@dagent.tool` binding directly in an agent's `capabilities`, the
runner registers it while resolving that agent.

## Skill Not Visible

Add the skill root to the runner and check the agent's `skills` filter:

```python
runner.add_skill_root("team-skills")
agent = dagent.ToolAgent(profile="conversation", skills=["writing/terse"])
```

Use `skills=None` to allow all configured skills, `skills=[]` to hide skill
tools, and `skills=[...]` to expose only named skills.

## Static DAG Validation Fails

Static DAG node output references do not infer edges. Add explicit dependencies:

```python
dag.add_edge(search_node, render_node)
```

Validation fails closed for non-upstream reads, unknown artifacts, malformed
expressions, unsafe artifact boundaries, and invalid control-flow references.

## Review Resume Fails

If persisted work is awaiting review, restore the SDK checkpoint and continue
with `resume(..., checkpoint=...)`, not `run(..., state=...)`:

```python
restored = dagent.RunCheckpoint.model_validate_json(saved_json)
pending = restored.state.pending_review
if pending is None:
    raise RuntimeError("Checkpoint is not awaiting review")
result = await runner.resume(
    dagent.ReviewHandle(pending).approve(),
    checkpoint=restored,
)
```

`run(..., state=...)` and `resume(..., state=...)` do not exist in 0.8.
If checkpoint resume reports missing or disabled capability IDs or missing
skills, construct a compatible `Runner`; the SDK does not silently widen the
saved scope.

## Streaming Text Is Interleaved

Parallel DAG nodes can stream simultaneously. Group text deltas by
`response_id`, not only by event ordering or `model_step`.
