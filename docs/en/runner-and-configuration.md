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
    workspace="agent-workspace",
    runtime_directory=".runtime",
    provider=provider,
    capabilities=[],
    skill_roots=["team-skills"],
    profile_root="profiles",
    planner_frontend="typed_spec",
    mcp_stdio_stderr="discard",
    extra_system_prompt="Follow the host application's response policy.",
)
```

dagent is an in-process SDK. Construct and close `Runner` in the process you
control. Process commands, health, credentials, persistence, scheduling, and
container lifecycle are host responsibilities; the SDK intentionally exposes
no worker or service loop.

`Runner(...)` does not read `config.yaml` implicitly. When omitted, `workspace`
defaults to `Path.home() / ".dagent"` and `runtime_directory` defaults to
`.runtime`. `runtime_directory` must be a non-empty, traversal-free relative
path. Hosts that own storage or isolation boundaries should pass both values
explicitly and may put the workspace on tmpfs when runtime data should remain
memory-backed. `Runner.from_config(...)` uses the same runtime-path defaults;
they are not loaded from YAML.

Each run records its own directory under `<workspace>/runs/<run_id>`. In
ToolAgent and DagAgent message runs, built-in file and shell tool relative paths
resolve from the current run workspace. SDK-private data is isolated under the
chosen relative directory:

- `<workspace>/<runtime_directory>/conversations` stores content-addressed
  resources needed by later conversation turns.
- `<run-workspace>/<runtime_directory>/results` stores externalized tool and MCP
  output.
- `<run-workspace>/<runtime_directory>/history` holds resources restored into a
  new run workspace.

These directories are lazy. Constructing a runner, running text-only turns, and
keeping small results inline do not create them.

When an application already owns the execution directory, pass
`workspace_path=...` to `Runner.run(...)` or `Runner.stream(...)`. This uses that
exact directory for the run instead of creating a `<run_id>` subdirectory. This
is a runtime workspace selection feature, not persistence; the caller remains
responsible for storing conversation and review state outside the SDK. A
conversation continuation is a new run and may select a different workspace.
A review continuation uses the workspace frozen in its `RunCheckpoint`;
`Runner.resume(...)` does not accept a replacement workspace.

For profile-backed model calls, the SDK also adds a dynamic `Runtime Context`
section to the system prompt with the resolved workspace root and tells the
agent to resolve relative file paths from it. This applies to tool agents,
dynamic DAG planners, registered DAG subagents, and result validators. The
profile Markdown remains unchanged; runtime paths are never written into or
substituted into profile content. Lower-level profile-backed helpers such as
`FeedbackLearnerAgent` use the same section when their call receives
`workspace_path`.

The model receives the resolved run workspace in that system section. Uploaded
attachments and externalized results are represented in conversation input by
workspace-relative paths, media type, byte count, and digest, so file tools can
open them without exposing absolute paths or the runner-level conversation
backing store.

`extra_system_prompt` adds one literal, runner-wide instruction string without
replacing the agent profile or `Runtime Context`. The SDK assembles profile,
runtime context, extra prompt, and then dynamic tool, capability-catalog, and DAG
schema content in that order. The value applies to `ToolAgent`, the selected
tool or DAG execution path of `AutoAgent`, initial DAG planning and replanning,
and registered agents. It does not apply to `ValidatorAgent`,
`FeedbackLearnerAgent`, or AutoAgent's routing classifier.

Pass `None` to retain the existing prompt exactly. A configured value must be a
non-blank string of at most 16,384 characters. It is inserted literally: it is
not a Jinja template and has no targets or prompt-extension semantics. The
string only changes model instructions; it does not grant capabilities, expand
boundaries, bypass review, or change workspace permissions.

Each run freezes its initial value in `ResolvedRunPlan`. Review continuation
therefore uses the checkpointed value even if another runner, or the original
runner's later configuration, has a different `extra_system_prompt`.

## Provider Options

`dagent.Provider` targets OpenAI-compatible chat completions endpoints:

```python
provider = dagent.Provider(
    base_url="https://api.deepseek.com",
    model="deepseek-v4-pro",
    api_key_env="DEEPSEEK_API_KEY",
    timeout_seconds=60,
    reasoning={
        "enabled": True,
        "effort": "high",
        "budget_tokens": 1024,
        "capture": "field_and_tags",
    },
    stream_include_usage=False,
    context_window_tokens=32768,
    output_reserve_tokens=4096,
    extra_request_args={},
    extra_body={},
)
```

Use `reasoning` for common reasoning controls. Use `extra_request_args` and
`extra_body` only for provider-specific parameters supported by the target
endpoint.

Reasoning fields and `<think>` tags are captured separately and are not replayed
into later model input. Context and output-reserve values are required because
many private OpenAI-compatible endpoints do not report their model limits.
Streaming usage metadata is disabled by default because some compatible
endpoints reject `stream_options`. Set `stream_include_usage=True` only when the
target endpoint supports OpenAI's streamed usage extension; usage remains
optional in SDK results.
`capture="field_and_tags"` records both dedicated reasoning fields and tag
content. `capture="field"` trusts only the dedicated field and discards tag
content; tags are never left in visible content.

For structured planner calls, dagent counts the runtime JSON Schema in the
request budget and validates the returned object locally. The built-in
OpenAI-compatible provider requests
`{"type": "json_object"}`; it does not send `response_format.type="json_schema"`
or select behavior from provider or model names.

`timeout_seconds` controls the provider request timeout. Tool-agent and dynamic
DAG planning/replanning calls retry failed or timed-out LLM requests up to five
times, waiting `1`, `2`, `5`, `10`, then `30` seconds before retrying. If a
streaming response has already emitted tokens, dagent does not retry that
request because doing so would duplicate partial output. MCP server
`tool_timeout` is separate and only controls MCP tool calls.

## Configuration Files

Use `Runner.from_config(...)` when provider settings, MCP servers, validation,
or profile directories should come from YAML:

```python
runner = dagent.Runner.from_config(
    "config.yaml",
    workspace="agent-workspace",
    runtime_directory=".runtime",
    capabilities=[search],
    extra_system_prompt="Follow the host application's response policy.",
)
```

`extra_system_prompt` remains an explicit SDK argument; it is not loaded from
the YAML file.

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
planner_frontend: typed_spec
```

If no path is passed, `Runner.from_config(...)` resolves `DAGENT_CONFIG` or
`./config.yaml`. Relative `profiles.directory` values resolve from the config
file directory.

`planner_frontend` selects the global dynamic-DAG authoring frontend. The
default `typed_spec` asks the provider for a typed planner graph.
`sdk_builder` asks for constrained public Builder source, parses it without
executing Python, and immediately normalizes it to canonical `DAGSpec`:

```python
runner = dagent.Runner(workspace="agent-workspace", runtime_directory=".runtime", provider=provider, planner_frontend="sdk_builder")
```

The Builder frontend packages and explicitly injects a version-locked
`generate-dag` skill. It supports initial planning and full-spec replanning.
Both frontends share capability resolution, validation, review, and execution.

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
  jwt_secret: "onlyoffice-jwt-secret"
  lang: "zh-CN"
  project_file_edit_enabled: false
  run_artifact_edit_enabled: false
```

The WebUI model list includes the project `config.yaml` provider plus user
`model_providers`. If `active_model` names a user model, the backend rebuilds
the runner with that provider; otherwise the project `config.yaml` provider
remains the default. The WebUI registers both project and user MCP servers.
Project `mcp_servers` win on name conflicts, and conflicting user MCP entries
are reported instead of silently overriding project configuration.
The backend also applies the project-level `planner_frontend` when rebuilding a
runner for a user-selected model. It is a service-wide setting; message requests
and the WebUI do not expose a per-request override.

`python_tools` entries do not make `Runner.from_config(...)` import files
implicitly. Hosts that persist this section should load it explicitly with
`Runner.reload_python_tool_sources(...)` or the lower-level helpers in
`dagent.capabilities.python_tools`. A `path` entry points to a `.py` file visible
to the host process and `names` lists the exported objects to register. Each
named object must be created with `@dagent.tool`, so it is a
`CapabilityBinding` with a `tool.<function_name>` capability id. The WebUI also
supports uploading a `.py` file; uploads are copied to
`~/.dagent/python-tools/` and stored as `source: "managed"` entries in this
same user config file. A `module` entry imports an installed Python module by
name. Python tool reload invalidates import caches, but it does not reload
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

`onlyoffice` is optional and is used by the local WebUI document preview and
editing surface. `document_server_url` points to the ONLYOFFICE Document Server
that the browser can load. `public_api_base` must point to this FastAPI backend
at an address the Document Server can reach, because generated preview configs
contain signed file and callback URLs under that base. If the Document Server
has JWT enabled, `jwt_secret` must match its JWT secret; the backend signs the
generated editor config with HS256 and sends it as the ONLYOFFICE `token`. When
`onlyoffice.enabled` is false or the URLs are missing, the WebUI falls back to
its built-in browser preview path.
The same settings can be edited in the WebUI under
System Management -> Document Configuration.

By default, DOCX, XLSX, and PPTX files open through ONLYOFFICE in view mode. Set
`project_file_edit_enabled` to allow project files to open in edit mode, and set
`run_artifact_edit_enabled` to allow run artifacts to open in edit mode. Edit
mode disables ONLYOFFICE autosave and enables force-save, so only the user's
explicit Save action overwrites the original file. For run artifacts, this
changes the file in the run workspace; it does not rewrite historical trace
events or rerun the agent. File list responses include a `version` value derived
from file size and nanosecond mtime so the WebUI can refresh cached previews
only when the backing file changes. View-only ONLYOFFICE previews keep a small
browser-side cache of recently opened editor instances to make switching between
unchanged documents faster.

`api_key_env` is the recommended way to configure secrets. The WebUI can save a
literal `api_key` to `~/.dagent/config.yaml` only when the user explicitly
chooses to save it. The file is written with owner-only permissions where the
platform supports it, and API responses continue to redact saved secrets.

## Runtime Registration

Register tools, skill roots, and MCP servers when the runner is constructed:

```python
runner = dagent.Runner(
    workspace="agent-workspace",
    runtime_directory=".runtime",
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
host environment. Both transports support `connect_timeout` for registration
startup, defaulting to `60` seconds, and `tool_timeout` for tool calls,
defaulting to `300` seconds. HTTP servers also use those values for the HTTP
client connect and read timeouts.

`Runner` discards stdio MCP server stderr by default and does not create an SDK
log file. A host that already supervises process stderr can opt into forwarding
with `Runner(..., mcp_stdio_stderr="inherit")`, including when using
`Runner.from_config(...)`. Inherited stderr may contain credentials or other
sensitive server output, so the host must bound, sanitize, or discard it. This
setting is an explicit host policy and is not loaded from `config.yaml`.

MCP requires the optional extra:

```bash
pip install "dagent-ai[mcp]"
```

MCP registration is all-or-nothing: if a server fails to connect or any
discovered tool cannot register, the runner rolls back the capabilities from
that registration attempt.

Hosts that batch-reload MCP records can use
`runner.reload_mcp_servers_with_snapshots(...)` to receive successful
`MCPServerSnapshot` objects and per-server errors in one SDK result.

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

Use `runner.catalog_view()` when a host needs a read-only preview of the actual
runner-owned registration state:

```python
view = runner.catalog_view()
print([definition.id for definition in view.capabilities])
print([server.name for server in view.mcp_servers])
```

The view contains public `CapabilityDefinition` objects and MCP snapshots, not
handler objects or catalog internals. Hosts are still responsible for RBAC,
redaction, and policy filtering before returning the view to users.

Hosts that persist user-selected tool ids can call
`Runner.validate_capability_refs(...)` before constructing an agent or run
target. The method never registers `@dagent.tool` bindings and returns
`ValidationResult` issues with `capability_id` and `code` fields.

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
    workspace="agent-workspace",
    runtime_directory=".runtime",
    provider=provider,
    skill_roots=skill_roots,
    mcp_servers=mcp_servers,
    profile_root=profile_root,
)
```

If the new runtime is an overlay of an existing runner, use
`runner.derive(...)` instead of copying runtime internals:

```python
derived = runner.derive(
    workspace="effective-workspace",
    provider=provider,
    capabilities=python_tool_bindings,
    inherit_local_tools=True,
    exclude_local_tool_ids=config_managed_python_tool_ids,
    mcp_servers=mcp_servers,
    agents=agent_presets,
    profile_root=profile_root,
)
```

Derived runners inherit `runtime_directory`; pass a different safe relative
value only when the derived runtime deliberately uses another private layout.

The derived runner has its own catalog, MCP registrations, skill roots, agent
registrations, sandbox config, and validation settings. It reuses the base
provider by default unless `provider=` is passed. `inherit_local_tools=True`
copies local tools that were registered from `CapabilityBinding` objects, such
as `@dagent.tool` functions, without copying raw `register_capability(...)`
entries. Use `exclude_local_tool_ids` for tool ids that the derived runner will
install through another explicit path.

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
runner = dagent.Runner(workspace="agent-workspace", runtime_directory=".runtime", provider=provider, profile_root="profiles")
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
