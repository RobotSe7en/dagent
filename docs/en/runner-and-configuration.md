# Runner and Configuration

For ready-file notifications and incremental host persistence, see
[Conversation Changes](conversation-changes.md).

For result display budgets, typed retention metadata and file/search pagination, see
[Tool Result Recovery](tool-result-recovery.md).

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
`.runtime`. `runtime_directory` accepts an absolute path or a non-empty,
traversal-free relative path. Hosts that own storage or isolation boundaries should pass both values
explicitly and may put the workspace on tmpfs when runtime data should remain
memory-backed. `Runner.from_config(...)` uses the same runtime-path defaults;
they are not loaded from YAML.

In the **Unreleased** SDK, all runs of one conversation share one workspace,
`<workspace>/runs/conversation_<key>`, where `<key>` is the first 32 hex digits
of the SHA-256 of `ConversationState.id`. Pass the preceding
`ConversationState` back, with its `id`, to keep using that directory; a state
rebuilt with a new id gets a new one. A run without a `conversation` starts a
new conversation and so gets a new directory. Static DAG runs, which have no
conversation, still use `<workspace>/runs/<run_id>`. In ToolAgent and DagAgent
message runs, built-in file and shell tool relative paths resolve from the
current run workspace. SDK-private data is isolated under the chosen relative
directory:

- `<run-workspace>/<runtime_directory>/results` stores tool and MCP output that
  overflowed its display limit, plus large values and binary artifacts.
- `<run-workspace>/<runtime_directory>/history` stores compaction archives
  (records, sources and legacy result indexes).

These directories are lazy. Constructing a runner or running text-only turns
does not create them. Display output overflows to a file only when the model
that reads it has the built-in `read_file` tool; otherwise the display is still
shortened, but no file is written. Shell output is written to a file only when it
exceeds the shell's own inline limits (100,000 bytes or 2,000 lines). Overflow
files are written best-effort, carry no digest, and are never checked or
reread by the SDK. See [tool result recovery](tool-result-recovery.md).
Uploaded attachments alone do not create a history directory.

The SDK copies nothing between workspaces. Because a conversation's runs share
its workspace, later turns read the files earlier turns wrote, compaction
archives included. Earlier versions also kept copies of compaction archives in
`<workspace>/<runtime_directory>/conversations` and restored them into each new
run workspace; that directory is no longer written or read and can be deleted.

### Store private runtime files outside the workspace

Pass an absolute `runtime_directory` to keep SDK-managed results and recovery
resources outside the model's working directory:

```python
runner = dagent.Runner(
    workspace="agent-workspace",
    runtime_directory="/srv/dagent-runtime",
    provider=provider,
)
```

The store contains `runs/<workspace-key>/{results,history}/`.
`workspace-key` is the SHA-256 of the resolved run workspace path, so separate
workspaces do not share their result files. Reusing the same workspace reuses
its storage namespace. A relative value keeps the existing layout; relative
parent traversal such as `../runtime` remains invalid. Absolute paths are
resolved when the runner is constructed. `from_config` accepts this explicit
argument, `derive` inherits it unless overridden, and review checkpoints freeze it.

Result references retain logical, workspace-relative `.runtime/...` paths.
Built-in `read_file`, `grep`, and `list_files` resolve those paths into the
external store while applying the existing read boundary. File tools cannot
write this logical namespace. No symlink or `.runtime` placeholder is created
in the workspace. Existing workspace symlinks under `.runtime` do not redirect
runtime references away from the external store. Shell commands operate on the
actual filesystem and cannot use those logical paths; use the file tools to
inspect retained results.
For host-side access use:

```python
saved_path = runner.resolve_runtime_path(
    reference.path, workspace_path=result.workspace_path,
    runtime_directory=result.plan.runtime_directory,
)
```

Continue conversations with the same absolute store and the same workspace
path, so the files of earlier turns, compaction archives included, stay
available: they live in the workspace's namespace, and a continuation in
another workspace keeps their paths but cannot read them. Existing stored files
are not automatically moved when this setting changes; resume old checkpoints
with their frozen storage setting. Hosts remain responsible for preserving the
store and persisting conversation/checkpoint records.

An absolute directory changes storage location, not process permissions.
Local shell commands retain the host process's access. Docker execution mounts
only the runner workspace's and current run workspace's
`runs/<workspace-key>/` directories read-write for tool workers; it does not mount
the shared `runs/` parent. Sandbox startup creates the mounted namespaces when
needed.
Use host permissions and sandbox policy when deletion must be prevented.

### Choose a run workspace

When an application already owns the execution directory, pass
`workspace_path=...` to `Runner.run(...)` or `Runner.stream(...)`. This uses that
exact directory for the run instead of the conversation's directory under
`workspace_root`. This is a runtime workspace selection feature, not
persistence; the caller remains responsible for storing conversation and review
state outside the SDK. A conversation continuation is a new run and may select a
different workspace; see [moving a conversation to another
workspace](conversation-changes.md#move-a-conversation-to-another-workspace).
A review continuation uses the workspace frozen in its `RunCheckpoint`;
`Runner.resume(...)` does not accept a replacement workspace.

Runs of one conversation that execute at the same time share its workspace. The
SDK does not lock it; overflow and archive file names never collide, but files
that tools write can. Workspaces accumulate per conversation; deleting them is
the host's decision.

For profile-backed model calls, the SDK also adds a dynamic `Runtime Context`
section to the system prompt with the resolved workspace root and tells the
agent to resolve relative file paths from it. This applies to tool agents,
dynamic DAG planners, registered DAG subagents, and result validators. The
profile Markdown remains unchanged; runtime paths are never written into or
substituted into profile content. Lower-level profile-backed helpers such as
`FeedbackLearnerAgent` use the same section when their call receives
`workspace_path`.

The model receives the resolved run workspace in that system section. Attachment
records and externalized results use workspace-relative paths. An attachment's
byte count describes its upload-time contents; attachments from earlier versions
also keep their upload-time digest. A tool-output reference only reports
where an overflow file was written; it has no digest and the file is not
checked, so a later read returns whatever the file contains then.

`extra_system_prompt` adds one literal, runner-wide instruction string without
replacing the agent profile or `Runtime Context`. The SDK assembles profile,
runtime context, extra prompt, and then dynamic skill-index, tool,
capability-catalog, and DAG schema content in that order. The value applies to
`ToolAgent`, the selected
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

### Editing uploaded files

Pass `input_uploads=[dagent.ArtifactUpload(filename="note.txt", content=b"draft")]`
to a message run to write `uploads/note.txt` inside its workspace. Message
uploads and static DAG artifact uploads are written once; a short write raises
`OSError` before agent execution. In the **Unreleased** SDK the written file is
not read back, and new `Attachment` records carry no digest (`sha256` is
`None`). Only the byte count is recorded.

After upload, tools may modify, overwrite, or delete the working file. The SDK
does not recheck historical attachment metadata during review, completion, or
conversation continuation. `Attachment` retains upload-time metadata; it does
not assert that the file still exists or has those contents. A tool reading a
deleted file reports its normal file-access error. Because a conversation's runs
share its workspace, a later upload with the same file name overwrites the
earlier file, whose attachment record then describes contents that are gone.

For continued editing, pass the preceding `conversation` to the next run; it
uses the same workspace, or pass the same `workspace_path` when you choose one.
If you select another workspace, the caller must copy the desired working files
there: the SDK does not copy historical uploads, overflow files or compaction
archives, restore deleted attachments, or rebase paths. Keep original versions
in independent host-managed snapshots when needed.

Run [the editable uploads example](../../examples/editable_uploads.py) for an
offline upload, edit, and continuation using one explicit workspace.

## Provider Options

`dagent.Provider` targets private vLLM deployments and supports both OpenAI Chat
Completions and Responses:

```python
provider = dagent.Provider(
    base_url="http://localhost:8000/v1",
    model="Qwen/Qwen3-Coder",
    api_key="local",
    timeout_seconds=60,
    protocol="auto",
    token_counting="auto",
    chat_reasoning_field="auto",
    reasoning_effort="high",
    reasoning_mapping=None,
    reasoning_capture="field_and_tags",
    stream_include_usage=False,
    capture_cache_usage=False,
    context_window_tokens=None,
    max_output_tokens=None,
    extra_request_args={},
    extra_body={},
)
```

Construction performs no network I/O. On the first model call, exact token
count, or explicit `await provider.inspect_capabilities()`, the provider reads
vLLM's `/openapi.json` and `/version` and caches a typed capability report.
`protocol="auto"` prefers Responses only when it supports the current request's
tools, streaming, structured-output, and reasoning requirements. Otherwise it
selects Chat when Chat satisfies them. An inconclusive probe emits a warning
and selects Chat. An explicit
`protocol` never falls back, and a failed POST is never retried through the
other protocol.

Both protocols are stateless: dagent sends the complete selected context on
every call. Responses always uses `store=False` and never uses
`previous_response_id` or encrypted reasoning content. `reasoning_effort` maps
to the Chat `reasoning_effort` field or Responses `reasoning.effort`.
Token-based reasoning budgets are not supported. The old nested `reasoning`
object is rejected.

The optional `reasoning_mapping` configuration (since 0.9.19) adapts request field
paths and string effort names independently for Chat and Responses. Protocols
without a mapping keep their built-in behavior. See
[custom reasoning fields and effort names](model-context-and-reasoning.md#custom-reasoning-fields-and-effort-names).

`chat_reasoning_field` controls only replay serialization for Chat. `auto`
uses `reasoning_content` for recognized official DeepSeek V4 endpoints/models,
`reasoning` for a detected vLLM server, and omits replay otherwise. Explicit
`reasoning_content`, `reasoning`, and `omit` take precedence. Model names alone
do not identify a proxy protocol; configure its documented field explicitly. Response parsing accepts both common output field
names and normalizes them into `AssistantMessage.reasoning`.

`token_counting="auto"` uses vLLM `/tokenize` when advertised, including
messages and tool schemas. It emits a warning and uses the deterministic
heuristic when exact counting is unavailable. `"vllm"` makes a tokenize
failure explicit; `"heuristic"` skips probing for token counts. A discovered
`max_model_len` supplies the default total context limit. An explicit
`context_window_tokens` overrides discovery but is rejected when it exceeds a
successfully discovered server limit. Discovery failure warns and falls back to
131,072 (128K) when no value was configured. Official DeepSeek V4 models use
their documented 1M window through endpoint/model recognition, including with
heuristic counting; see [model context](model-context-and-reasoning.md#token-accounting-and-compaction).
`max_output_tokens` is optional; when set,
it is both reserved from input capacity and sent as the real generation limit.
When unset, no output-limit field is sent.

Streaming usage metadata is disabled by default because some compatible
endpoints reject `stream_options`. Set `stream_include_usage=True` only when the
target endpoint supports OpenAI's streamed usage extension; usage remains
optional in SDK results.

Since 0.10.0, the `capture_cache_usage=True` option records standard server-reported
cache counts in `ModelTokenUsage.cached_input_tokens`. It changes response parsing
only and does not enable streaming usage or server-side caching. With compatible
Chat streaming endpoints, enable `stream_include_usage=True` separately. See
[prompt cache usage](model-context-and-reasoning.md#prompt-cache-usage).

`reasoning_capture="field_and_tags"` records both dedicated reasoning fields
and tag content. `reasoning_capture="field"` trusts only the dedicated field
and discards tag content; tags are never left in visible content. Capture
controls response parsing only; it does not enable reasoning in the request.

For structured planner calls, dagent validates the returned object locally and
maps the full schema to Chat `response_format.type="json_schema"` or Responses
`text.format.type="json_schema"`.

See [Model Context and Reasoning](model-context-and-reasoning.md) for replay
modes, wire examples, compaction order, and capability details.

`timeout_seconds` controls the provider request timeout. Tool-agent, dynamic DAG
planning/replanning, routing, result validation, and context compaction retry
transient LLM request failures up to five times, waiting `1`, `2`, `5`, `10`, then
`30` seconds before retrying. If a streaming response has already emitted tokens,
dagent does not retry that request because doing so would duplicate partial
output. MCP server
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
  base_url: "http://localhost:8000/v1"
  model: "Qwen/Qwen3-Coder"
  api_key: "local"
  protocol: auto
  token_counting: auto
  chat_reasoning_field: auto
  reasoning_effort: "high"
  reasoning_capture: field_and_tags
  context_window_tokens: null
  max_output_tokens: null
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
User model entries also accept [`reasoning_mapping`](model-context-and-reasoning.md#custom-reasoning-fields-and-effort-names)
(since 0.9.19); the WebUI passes it to the provider and preserves it when selecting or saving models.
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

Derived runners inherit `runtime_directory`; pass another safe relative path or
an absolute storage directory when the derived runtime needs a different layout.

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
Tool runs that exhaust their step limit can still receive validation feedback and
retry to supply a final answer. Terminal model failures bypass validation.
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

## Optional context observations

Since 0.10.1, pass `context_observability=True` to `Runner` or
`Runner.from_config` to observe ToolAgent HTTP calls and cache-aware context
maintenance through `stream` / `resume_stream`. The default is `False`;
this flag changes no generation payload, policy, checkpoint, or saved SDK state.
When enabled, legacy summaries use internal streaming to expose real progress.
See [stream observations](results-streaming-review.md#context-and-request-observations).

## Bound retained root-run state

`Runner(max_retained_runs=None)` keeps all ended runs, preserving the default.
Set `0` to release their heavy state at root run/resume completion, or a positive
integer to retain the most recently ended root runs. Active and awaiting-review
runs do not count and are never evicted. A run that ends after review counts as
newly ended, even if it started much earlier. Cleanup includes ordinary and
exceptional exits, after delivery and failure state have been finalized.

The limit releases saved `RunState`, cached checkpoints, stale review indexes,
and DAG node conversations owned by this Runner. External providers own their
own caches. Workspaces, result files and compaction archives remain available.
Returned results and exported checkpoints remain valid; resume uses an explicitly
supplied checkpoint, independently of this cache.

Pass `max_retained_runs` explicitly to `Runner.from_config(...)`; this process
policy is not loaded from YAML and enters no execution fingerprint.
`runner.derive()` inherits the limit with empty history;
`runner.derive(max_retained_runs=None)` explicitly restores unlimited retention.

This bounds heavy ended-run state, not total process memory. With a limit, used
run ids and consumed review ids remain to prevent replay and grow with history; a
used `run_id` cannot be reused after eviction. With unlimited retention, a saved
state already blocks reuse, so a run that ended before saving any state can be
retried with the same `run_id`; ids of persistent review states released after
acknowledgement stay used. Caller-held results, event queues,
provider/host logs and within-run `new_items` can also consume memory. See
[the offline example](../../examples/run_retention.py).

For persistent ToolLoop streams, review checkpoints are host-owned: after the
review terminal batch is acknowledged, local heavy review state is released
regardless of the ordinary retention limit. Completed persistent runs do not cache
checkpoints. Use `max_retained_runs=0` for host-owned ordinary history. See
[persistent streams](conversation-changes.md).
