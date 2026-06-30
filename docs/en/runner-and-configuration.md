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

## User WebUI Configuration

The local FastAPI/WebUI backend also reads and writes a user-level config at
`~/.dagent/config.yaml`. This file is for local UI state and user defaults; it
does not change what `Runner.from_config(...)` loads in SDK code.

User config uses the same YAML style for provider-shaped model entries and MCP
servers, but it is scoped to WebUI-managed models, the active WebUI model, user
MCP servers, explicitly imported Python tools, and local WebUI artifact preview
settings:

```yaml
model_providers:
  local-qwen:
    name: "Local Qwen"
    base_url: "http://localhost:8000/v1"
    model: "qwen3-coder"
    api_key_env: "LOCAL_QWEN_API_KEY"
active_model: "local-qwen"
mcp_servers:
  local_fs:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
python_tools:
  - id: "local_docs"
    source: "path"
    path: "/Users/olivia/tools/local_tools.py"
    names: ["search_docs", "summarize_page"]
    enabled: true
  - id: "package_tools"
    source: "module"
    module: "my_project.tools"
    names: ["lookup"]
    enabled: true
onlyoffice:
  enabled: true
  document_server_url: "http://192.168.31.219:8089"
  public_api_base: "http://192.168.31.10:8000"
  lang: "zh-CN"
```

The WebUI model list includes the project `config.yaml` provider plus user
`model_providers`. If `active_model` names a user model, the backend rebuilds
the runner with that provider; otherwise the project `config.yaml` provider
remains the default. The WebUI registers both project and user MCP servers.
Project `mcp_servers` win on name conflicts, and conflicting user MCP entries
are reported instead of silently overriding project configuration.

`python_tools` entries are loaded only by the local WebUI backend. They do not
make `Runner.from_config(...)` import files implicitly. A `path` entry points to
a `.py` file visible to the backend process and `names` lists the exported
objects to register. Each named object must be created with `@dagent.tool`, so
it is a `CapabilityBinding` with a `tool.<function_name>` capability id. The
WebUI also supports uploading a `.py` file; uploads are copied to
`~/.dagent/python-tools/` and stored as `source: "managed"` entries in this
same user config file. A `module` entry imports an installed Python module by
name. `/python-tools/reload` invalidates import caches, but it does not reload
modules already present in `sys.modules`; use `path` or uploaded `managed`
sources for reload-style development.

Reloading Python tools through the WebUI rebuilds only the imported Python-tool
capabilities. It does not restart the whole runner or reconnect unrelated MCP
servers. If a removed or disabled Python tool was referenced by an agent preset,
that preset is reported as an agent error until its capabilities are updated.

Python files are imported as local code, so top-level module code runs during
loading. The WebUI never scans directories or registers every object
automatically; it loads only explicit config entries and explicit `names`.
Import failures, missing names, non-`@dagent.tool` exports, and capability id
collisions are reported in the tool management page without failing backend
startup.

`onlyoffice` is optional and is used only by the local WebUI artifact preview.
`document_server_url` points to the ONLYOFFICE Document Server that the browser
can load. `public_api_base` must point to this FastAPI backend at an address the
Document Server can reach, because generated preview configs contain signed file
and callback URLs under that base. When `onlyoffice.enabled` is false or the
URLs are missing, the WebUI falls back to its built-in browser preview path.
The same settings can be edited in the WebUI under
System Management -> OnlyOffice Configuration.

`api_key_env` is the recommended way to configure secrets. The WebUI can save a
literal `api_key` to `~/.dagent/config.yaml` only when the user explicitly
chooses to save it. The file is written with owner-only permissions where the
platform supports it, and API responses continue to redact saved secrets.

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

The local `/capabilities` mutation API is a runtime host/debug surface for raw
capability definitions. User-managed Python tools should be added and persisted
through `/python-tools` or the WebUI import flow, not through template-backed
runtime capability creation.

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
remains the default model unless the user activates a persisted WebUI model from
`~/.dagent/config.yaml`.

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
