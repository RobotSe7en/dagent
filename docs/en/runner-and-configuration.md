# Runner and Configuration

`Runner` is the public SDK entrypoint for executing agents and DAGs. It owns
provider wiring, capability registration, skill store access, MCP registration,
runtime state, and review resume flow.

## Direct SDK Construction

Use direct construction when your application already has configuration in code:

```python
import dagent


provider = dagent.Provider(
    base_url="https://api.openai.com/v1",
    model="your-model",
    api_key_env="OPENAI_API_KEY",
)
runner = dagent.Runner(
    workspace=".dagent",
    provider=provider,
    capabilities=[],
    skill_roots=["team-skills"],
    profile_root="profiles",
)
```

`Runner(...)` does not read `config.yaml` implicitly.

The runner workspace defaults to `.dagent`, and each run records its own
directory under `.dagent/runs/<run_id>`. In ToolAgent and DagAgent message runs,
built-in file and shell tool relative paths resolve from the current run
workspace. Passing `workspace=...` uses that exact directory as the dagent
runtime workspace, so run workspaces live under `<workspace>/runs/<run_id>` by
default.

## Provider Options

`dagent.Provider` targets OpenAI-compatible chat completions endpoints:

```python
provider = dagent.Provider(
    base_url="https://api.deepseek.com",
    model="deepseek-v4-pro",
    api_key_env="DEEPSEEK_API_KEY",
    timeout_seconds=60,
    strip_thinking=False,
    reasoning={"enabled": True, "effort": "high", "budget_tokens": 1024},
    extra_request_args={},
    extra_body={},
)
```

Use `reasoning` for common reasoning controls. Use `extra_request_args` and
`extra_body` only for provider-specific parameters supported by the target
endpoint.

## Configuration Files

Use `Runner.from_config(...)` when provider settings, MCP servers, validation,
or profile directories should come from YAML:

```python
runner = dagent.Runner.from_config(
    "config.yaml",
    workspace=".dagent",
    capabilities=[search],
)
```

Example `config.yaml`:

```yaml
provider:
  base_url: "https://api.deepseek.com"
  model: "deepseek-v4-pro"
  api_key_env: "DEEPSEEK_API_KEY"
  reasoning:
    enabled: true
    effort: "high"
    budget_tokens: 1024
profiles:
  directory: "profiles"
enable_result_validation: false
mcp_servers:
  fs:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
  remote_docs:
    transport: "http"
    url: "https://mcp.example.com/mcp"
    headers:
      Authorization: "Bearer ${MCP_TOKEN}"
```

If no path is passed, `Runner.from_config(...)` resolves `DAGENT_CONFIG` or
`./config.yaml`. Relative `profiles.directory` values resolve from the config
file directory.

## Runtime Registration

Register tools, skill roots, and MCP servers when the runner is constructed:

```python
runner = dagent.Runner(
    workspace=".dagent",
    provider=provider,
    capabilities=[search],
    skill_roots=["team-skills"],
    mcp_servers={
        "fs": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        },
        "remote_docs": {
            "transport": "http",
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
        },
    },
)
```

MCP server names are dagent local workspace keys, not third-party MCP tool
names. The local `/mcp/servers` API enforces letters, numbers, and underscores
for this key, for example `remote_docs`; use the same convention for
`mcp_servers` and `runner.add_mcp_server(...)` to keep ids predictable.
Third-party MCP tool names are preserved in capability config and canonicalized
when dagent builds `mcp.*` capability ids.

You can also register resources after construction:

```python
runner.add_tool(search)
runner.add_skill_root("more-skills")
runner.add_agent(dagent.ToolAgent(
    profile="conversation",
    name="research_helper",
    capabilities=["tool.search"],
    skills=["research/briefing"],
))

mcp_definitions = runner.add_mcp_server(
    "team_fs",
    {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
    },
)

print([definition.id for definition in mcp_definitions])

runner.replace_mcp_server(
    "team_fs",
    {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "docs"],
    },
)
runner.remove_mcp_server("team_fs")
```

MCP server configs support two transports. Omit `transport` for stdio servers
that launch a local command. Use `transport: "http"` with `url` for Streamable
HTTP servers. HTTP `headers` values expand `${ENV_NAME}` references from the
host environment.

MCP requires the optional extra:

```bash
pip install "dagent-ai[mcp]"
```

MCP registration is all-or-nothing: if a server fails to connect or any
discovered tool cannot register, the runner rolls back the capabilities from
that registration attempt.

`runner.add_agent(...)` registers a leaf `ToolAgent` as `agent.<name>`. The
registered agent can be exposed to top-level `ToolAgent`, `AutoAgent`, and
`DagAgent` runs through their `agents` field. Registered subagents cannot expose
other subagents.

## Capability Management

Hosts such as WebUI backends can manage raw capability definitions:

```python
runner.register_capability(definition, handler, supports_context=False)
runner.replace_capability(definition, handler)
runner.set_capability_enabled("tool.search", False)
result = await runner.test_capability("tool.search", {"q": "dagent"})
runner.remove_capability("tool.search")

for definition in runner.list_capabilities(kind="mcp"):
    print(definition.id)
```

## Runtime Provider Switching

Hosts that let users choose a model at runtime should create a new public
provider and rebuild the runner instead of mutating runner internals:

```python
runner.close()
provider = dagent.Provider(
    base_url="https://api.openai.com/v1",
    model="another-model",
    api_key_env="OPENAI_API_KEY",
)
runner = dagent.Runner(
    workspace=".dagent",
    provider=provider,
    skill_roots=skill_roots,
    mcp_servers=mcp_servers,
    profile_root=profile_root,
)
```

The local WebUI model manager follows this pattern. The `config.yaml` provider
remains the default model. Runtime-added model entries are session state unless
the host application chooses to persist them.

## Validation

`enable_result_validation` in config sets the initial default. Runtime controls
can override it for the current session:

```python
runner.enable_validation = True
```

Result validation runs for tool and DAG outcomes that include execution context.
Plain chat-only responses are not validated.

## Profiles

Built-in profiles are packaged resources. User profile directories are explicit:

```python
runner = dagent.Runner(provider=provider, profile_root="profiles")
agent = dagent.ToolAgent(profile="reviewer")
```

Read packaged built-ins when you need to inspect or display them:

```python
profile = dagent.load_builtin_profile("conversation")
available = dagent.list_builtin_profiles()
```

## Lifecycle

Call `runner.close()` when you are done with a runner, especially after MCP
servers have been registered.

Runtime inspection helpers:

```python
trace = runner.run_trace(run_id)
state = runner.run_state(run_id)
```
