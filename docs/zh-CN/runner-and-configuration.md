# Runner 和配置

`Runner` 是执行 agents 和 DAGs 的公开 SDK 入口。它拥有 provider wiring、capability
registration、skill store access、MCP registration、runtime state 和 review resume flow。

## 直接构造 SDK

当你的应用已经在代码中持有配置时，使用直接构造：

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
    extra_system_prompt="遵循宿主应用的回答策略。",
)
```

达智是进程内 SDK。请在你控制的进程中构造并关闭 `Runner`。进程命令、健康检查、
凭证、持久化、调度和容器生命周期由 host 负责；SDK 不提供 worker 或 service loop。

`Runner(...)` 不会隐式读取 `config.yaml`。省略时，`workspace` 默认为
`Path.home() / ".dagent"`，`runtime_directory` 默认为 `.runtime`。
`runtime_directory` 必须是非空、无路径穿越的相对路径。自行管理存储或隔离边界的 host
应显式传入这两个值；如果运行数据只需驻留内存，也可以把 workspace 放在 tmpfs。
`Runner.from_config(...)` 使用相同的运行路径默认值，不会从 YAML 加载它们。

每次运行默认在 `<workspace>/runs/<run_id>` 下记录自己的目录。在 ToolAgent 和
DagAgent message run 中，内置 file 和 shell tool 的相对路径从当前 run workspace
解析。SDK 私有数据隔离在调用方选择的相对目录中：

- `<workspace>/<runtime_directory>/conversations` 保存后续会话轮次需要的内容寻址资源；
- `<run-workspace>/<runtime_directory>/results` 保存外置的 tool/MCP 输出；
- `<run-workspace>/<runtime_directory>/history` 保存恢复到新 run workspace 的资源。

这些目录按需创建。只构造 runner、执行纯文本轮次或保持小结果内联都不会创建它们。

如果应用已经拥有执行目录，可以给 `Runner.run(...)` 或 `Runner.stream(...)` 传入
`workspace_path=...`。达智会直接使用这个目录运行，不再创建 `<run_id>` 子目录。
这是运行时 workspace 选择能力，不是持久化能力；调用方仍然负责在 SDK 之外保存 run
的 conversation 和 review 状态。会话续聊会创建新的 run，也可以选择不同 workspace。
review 续跑使用 `RunCheckpoint` 中冻结的 workspace；`Runner.resume(...)` 不接受替换
workspace。

对于由 profile 驱动的模型调用，SDK 还会在 system prompt 中动态加入
`Runtime Context` 段，写明解析后的 workspace root，并要求 agent 从该目录解析相对
文件路径。这适用于 tool agent、动态 DAG planner、注册到 DAG 的子 agent 和结果
validator。Profile Markdown 本身保持不变；runtime path 不会写入 profile 内容，也不会
对 profile 做模板替换。`FeedbackLearnerAgent` 等底层 profile-backed helper 在调用时
收到 `workspace_path` 后，也会使用相同的动态段。

模型会在该 system 段中收到解析后的 run workspace。上传附件和外置结果会以 workspace
相对路径、媒体类型、字节数和摘要出现在 conversation input 中，因此文件工具可以打开
它们，同时不会暴露绝对路径或 runner-level conversation backing store。

`extra_system_prompt` 用一个普通字符串统一追加宿主指令，不会替换 Agent Profile 或
`Runtime Context`。SDK 的组装顺序是：Profile、Runtime Context、Extra System Prompt，
然后才是动态 skill index、tool、capability catalog 和 DAG schema 内容。它适用于 `ToolAgent`、
`AutoAgent` 实际选择的 tool 或 DAG 执行路径、DAG 初始规划与 replan，以及 registered
agent；不适用于 `ValidatorAgent`、`FeedbackLearnerAgent` 和 AutoAgent 的路由分类器。

传入 `None` 时，system prompt 与现有行为完全相同。非空值必须是去除空白后仍有内容、
且不超过 16,384 个字符的字符串。SDK 按字面注入它，不执行 Jinja 模板，也不提供
targets 或 prompt extension 语义。它只影响模型指令，不会授予 capability、扩大
boundary、绕过 review 或改变 workspace 权限。

每个 run 会把初始值冻结在 `ResolvedRunPlan` 中。因此 review 续跑始终使用 checkpoint
里的值，即使另一个 runner 或原 runner 后续配置了不同的 `extra_system_prompt`。

## Provider 选项

`dagent.Provider` 面向 OpenAI-compatible chat completions endpoints：

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

使用 `reasoning` 传递常见 reasoning controls。只有当目标 endpoint 支持对应字段时，
才使用 `extra_request_args` 和 `extra_body` 传递 provider-specific 参数。

reasoning 字段和 `<think>` tag 会被单独捕获，不会回放到后续模型输入。由于很多私有
OpenAI-compatible endpoint 不会报告模型限制，因此需要显式配置上下文和输出预留。
流式 usage metadata 默认关闭，因为部分兼容 endpoint 会拒绝 `stream_options`。
只有目标 endpoint 支持 OpenAI 的流式 usage 扩展时才设置
`stream_include_usage=True`；SDK 结果中的 usage 始终是可选值。
`capture="field_and_tags"` 会记录专用 reasoning 字段和 tag 内容；
`capture="field"` 只信任专用字段并丢弃 tag 内容；tag 不会残留在可见正文中。

对于 structured planner call，达智会把 runtime JSON Schema 计入请求预算，并在本地
校验返回对象。内置 OpenAI-compatible provider 固定请求
`{"type": "json_object"}`，不再发送 `response_format.type="json_schema"`，也不会根据
provider 或 model 名称选择不同路径。

`timeout_seconds` 控制 provider request timeout。Tool-agent 和动态 DAG 的 planning/replanning
LLM 调用会在请求失败或超时时最多重试 5 次，重试前分别等待 `1`、`2`、`5`、`10`、`30` 秒。
如果 streaming response 已经输出 token，达智不会重试这次请求，以避免重复输出部分内容。
MCP server 的 `tool_timeout` 是单独配置，只控制 MCP 工具调用。

## 配置文件

当 provider settings、MCP servers、validation 或 profile directories 应从 YAML 加载时，
使用 `Runner.from_config(...)`：

```python
runner = dagent.Runner.from_config(
    "config.yaml",
    workspace="agent-workspace",
    runtime_directory=".runtime",
    capabilities=[search],
    extra_system_prompt="遵循宿主应用的回答策略。",
)
```

`extra_system_prompt` 始终是显式 SDK 参数，不会从 YAML 文件中加载。

示例 `config.yaml`：

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

如果没有传入 path，`Runner.from_config(...)` 会解析 `DAGENT_CONFIG` 或
`./config.yaml`。相对的 `profiles.directory` 会从配置文件所在目录解析。

`planner_frontend` 用于全局选择 dynamic DAG authoring frontend。默认的 `typed_spec`
要求 provider 返回类型化 planner graph；`sdk_builder` 要求返回受限的公开 Builder source，
host 不执行 Python，而是解析后立即规范化为 canonical `DAGSpec`：

```python
runner = dagent.Runner(workspace="agent-workspace", runtime_directory=".runtime", provider=provider, planner_frontend="sdk_builder")
```

Builder frontend 会打包并显式注入 version-locked `generate-dag` skill，支持初始规划和
full-spec replan。两个 frontend 共用 capability resolution、validation、review 和 execution。

## WebUI 用户配置

本地 FastAPI/WebUI backend 还会读写用户级配置 `~/.dagent/config.yaml`。这个文件用于本机
UI 状态和用户默认值；它不会改变 SDK 代码中 `Runner.from_config(...)` 的加载行为。

用户配置复用 provider-shaped 模型条目和 MCP servers 的 YAML 风格，但范围限定为
WebUI 管理的模型、当前 WebUI 模型、用户 MCP servers、显式导入的 Python tools，以及
本地 WebUI artifact 预览设置：

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

WebUI 的模型列表包含项目 `config.yaml` provider 和用户配置中的 `model_providers`。
如果 `active_model` 指向一个用户模型，backend 会用该 provider 重建 runner；否则项目
`config.yaml` provider 仍是默认模型。WebUI 会同时注册项目和用户 MCP servers。同名冲突时，
项目 `mcp_servers` 优先；冲突的用户 MCP 条目会被报告，而不是静默覆盖项目配置。
Backend 使用用户选择的 model 重建 runner 时，也会应用项目级 `planner_frontend`。这是
service-wide setting；message request 和 WebUI 不提供 per-request override。

`python_tools` 条目不会让 `Runner.from_config(...)` 隐式 import 文件。持久化这一段配置的
host 应通过 `Runner.reload_python_tool_sources(...)` 或
`dagent.capabilities.python_tools` 中的底层 helpers 显式加载。`path` 条目指向 host 进程可访问
的 `.py` 文件，`names` 列出要注册的导出对象。每个对象都必须由 `@dagent.tool` 创建，
因此它是一个 `CapabilityBinding`，并使用 `tool.<function_name>` capability id。WebUI
也支持上传 `.py` 文件；上传文件会复制到 `~/.dagent/python-tools/`，并在同一个用户配置文件
中保存为 `source: "managed"` 条目。`module` 条目会按名称 import 已安装或可导入的
Python module。Python tool reload 会 invalidate import caches，但不会 reload 已在
`sys.modules` 中的 module；需要类似 reload 的开发体验时，请使用 `path` 或上传后的
`managed` source。

通过 WebUI reload Python tools 时，只会重建导入的 Python-tool capabilities。它不会重启
整个 runner，也不会重连无关 MCP servers。如果被删除或禁用的 Python tool 仍被某个 agent
preset 引用，该 preset 会显示为 agent 错误，直到它的 capabilities 被更新。

Python 文件会作为本地代码导入，因此模块顶层代码会在加载时执行。WebUI 不会扫描目录，
也不会自动注册文件中的所有对象；它只加载显式配置的条目和显式列出的 `names`。import
失败、名称缺失、非 `@dagent.tool` 导出以及 capability id 冲突都会显示在工具管理页，
不会导致 backend 启动失败。

`onlyoffice` 是可选配置，由本地 WebUI 文档预览与编辑界面使用。
`document_server_url` 指向浏览器能够加载的 ONLYOFFICE Document Server。
`public_api_base` 必须指向这个 FastAPI backend，并且要使用 Document Server 能访问到的
地址，因为生成的预览配置会在这个 base 下放入签名的文件 URL 和 callback URL。如果
Document Server 启用了 JWT，`jwt_secret` 必须和它的 JWT secret 一致；backend 会用
HS256 签名生成的编辑器配置，并作为 ONLYOFFICE `token` 传给前端。当
`onlyoffice.enabled` 为 false 或 URL 缺失时，WebUI 会回退到内置的浏览器预览路径。
同一组设置也可以在 WebUI 的“系统管理 -> 文档配置”中维护。

默认情况下，DOCX、XLSX 和 PPTX 文件会以 ONLYOFFICE view mode 打开。启用
`project_file_edit_enabled` 后，项目文件可以用 edit mode 打开；启用
`run_artifact_edit_enabled` 后，运行产物可以用 edit mode 打开。edit mode 会关闭
ONLYOFFICE autosave 并启用 force-save，因此只有用户显式点击 Save 时才会覆盖原文件。
对于运行产物，这会修改 run workspace 中的文件；不会重写历史 trace 事件，也不会重新
执行 agent。文件列表响应会包含根据文件大小和纳秒级 mtime 生成的 `version`，WebUI
据此只在底层文件变化时刷新缓存预览。view-only 的 ONLYOFFICE 预览会在浏览器侧保留少量
最近打开的编辑器实例，以加快在未变化文档之间切换的速度。

推荐用 `api_key_env` 配置密钥。只有当用户明确选择保存时，WebUI 才会把明文 `api_key`
写入 `~/.dagent/config.yaml`。在平台支持的情况下，该文件会以 owner-only 权限写入；
API 响应仍会 redact 已保存的密钥。

## 运行时注册

可以在 runner 构造时注册 tools、skill roots 和 MCP servers：

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

MCP server name 是达智的本地 workspace key，不是第三方 MCP tool 名。本地
`/mcp/servers` API 会强制这个 key 只能包含字母、数字和下划线，例如 `remote_docs`；
`mcp_servers` 和 `runner.add_mcp_server(...)` 也建议使用同样约定，让 id 保持可预测。
第三方 MCP tool 原始名称会保存在 capability config 中，并在达智生成 `mcp.*`
capability ids 时 canonicalize。

也可以在构造后注册资源：

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

MCP server config 支持两种 transport。本地命令型 stdio server 可以省略
`transport`。Streamable HTTP server 使用 `transport: "http"` 和 `url`。
HTTP `headers` 的值会展开 host 环境中的 `${ENV_NAME}` 引用。两种 transport
都支持 `connect_timeout` 控制注册启动等待时间，默认 `60` 秒；也支持
`tool_timeout` 控制工具调用时间，默认 `300` 秒。HTTP server 还会把这两个值用于
HTTP client 的 connect 和 read timeout。

`Runner` 默认丢弃 stdio MCP server 的 stderr，且不会创建 SDK 日志文件。已经在进程
边界监管 stderr 的 host 可以显式使用
`Runner(..., mcp_stdio_stderr="inherit")` 转发 stderr；
`Runner.from_config(...)` 也接受这个显式参数。转发的 stderr 可能包含凭据或其他敏感
server 输出，因此 host 必须对其限流、脱敏或丢弃。该设置属于显式 host policy，
不会从 `config.yaml` 加载。

MCP 需要可选依赖：

```bash
pip install "dagent-ai[mcp]"
```

MCP 注册是 all-or-nothing：如果 server 连接失败或任何已发现工具无法注册，runner 会回滚
这次注册尝试产生的 capabilities。

批量 reload MCP records 的 host 可以使用
`runner.reload_mcp_servers_with_snapshots(...)`，在一个 SDK result 中拿到成功的
`MCPServerSnapshot` 对象和逐 server errors。

`runner.add_agent(...)` 会把一个叶子 `ToolAgent` 注册为 `agent.<name>`。已注册 agent
可以通过顶层 `ToolAgent`、`AutoAgent` 和 `DagAgent` 的 `agents` 字段暴露出来。
已注册的子 agent 不能再暴露其他子 agent。

## Capability 管理

WebUI backend 这类 host 可以管理 raw capability definitions：

```python
runner.register_capability(definition, handler, supports_context=False)
runner.replace_capability(definition, handler)
runner.set_capability_enabled("tool.search", False)
result = await runner.test_capability("tool.search", {"q": "达智"})
runner.remove_capability("tool.search")

for definition in runner.list_capabilities(kind="mcp"):
    print(definition.id)
```

host 如果需要预览真实的 runner-owned 注册状态，应使用 `runner.catalog_view()`：

```python
view = runner.catalog_view()
print([definition.id for definition in view.capabilities])
print([server.name for server in view.mcp_servers])
```

这个 view 包含公开的 `CapabilityDefinition` 对象和 MCP snapshots，不暴露 handler 对象或
catalog 内部状态。把 view 返回给用户前，host 仍负责 RBAC、redaction 和 policy filtering。

持久化用户所选 tool ids 的 host 可以先调用
`Runner.validate_capability_refs(...)`，再构造 agent 或 run target。该方法不会注册
`@dagent.tool` bindings，并会返回带有 `capability_id` 和 `code` 字段的
`ValidationResult` issues。

本地 `/capabilities` mutation API 是 host/debug 用的 runtime raw capability surface。
用户管理的 Python tools 应通过 `/python-tools` 或 WebUI 导入流程添加和持久化，而不是通过
template-backed runtime capability creation。

## 运行时切换 Provider

如果 host 允许用户在运行时选择模型，应创建新的公开 provider 并重建 runner，
不要修改 runner 内部状态：

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

如果新 runtime 是已有 runner 的 overlay，应使用 `runner.derive(...)`，不要复制 runtime
内部状态：

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

派生 runner 默认继承 `runtime_directory`；只有派生 runtime 确实需要另一套私有布局时，
才传入不同的安全相对路径。

派生 runner 拥有自己的 catalog、MCP registrations、skill roots、agent registrations、
sandbox config 和 validation 设置。默认复用 base provider；传入 `provider=` 时使用新的
provider。`inherit_local_tools=True` 会复制通过 `CapabilityBinding` 注册的本地 tools，
例如 `@dagent.tool` 函数，但不会复制 raw `register_capability(...)` entries。
如果某些 tool ids 会通过另一个显式路径安装到派生 runner，请用
`exclude_local_tool_ids` 跳过它们。

本地 WebUI 的模型管理遵循这个模式。`config.yaml` 中的 provider 仍是默认模型。
除非用户激活了 `~/.dagent/config.yaml` 中持久化的 WebUI 模型。

## Validation

配置中的 `enable_result_validation` 设置初始默认值。运行时控制可以覆盖当前 session：

```python
runner.enable_validation = True
```

Result validation 运行在包含 execution context 的 tool 和 DAG outcomes 上。纯 chat-only
responses 不会被 validation。

## Profiles

内置 profiles 是打包资源。用户 profile 目录需要显式传入：

```python
runner = dagent.Runner(workspace="agent-workspace", runtime_directory=".runtime", provider=provider, profile_root="profiles")
agent = dagent.ToolAgent(profile="reviewer")
```

需要查看或展示内置 profiles 时，可以直接读取打包资源：

```python
profile = dagent.load_builtin_profile("conversation")
available = dagent.list_builtin_profiles()
```

## 生命周期

使用完 runner 后调用 `runner.close()`，尤其是在注册过 MCP servers 之后。

运行时检查 helpers：

```python
trace = runner.run_trace(run_id)
state = runner.run_state(run_id)
```
