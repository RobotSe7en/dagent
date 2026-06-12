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
    workspace=".",
    provider=provider,
    capabilities=[],
    skill_roots=["team-skills"],
    profile_root="profiles",
)
```

`Runner(...)` does not read `config.yaml` implicitly.

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
    workspace=".",
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
```

If no path is passed, `Runner.from_config(...)` resolves `DAGENT_CONFIG` or
`./config.yaml`. Relative `profiles.directory` values resolve from the config
file directory.

## Runtime Registration

Register tools, skill roots, and MCP servers when the runner is constructed:

```python
runner = dagent.Runner(
    workspace=".",
    provider=provider,
    capabilities=[search],
    skill_roots=["team-skills"],
    mcp_servers={
        "fs": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        },
    },
)
```

You can also register resources after construction:

```python
runner.add_tool(search)
runner.add_skill_root("more-skills")

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

MCP requires the optional extra:

```bash
pip install "dagent-ai[mcp]"
```

MCP registration is all-or-nothing: if a server fails to connect or any
discovered tool cannot register, the runner rolls back the capabilities from
that registration attempt.

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
