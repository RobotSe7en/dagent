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
    workspace=".dagent",
    provider=provider,
    capabilities=[],
    skill_roots=["team-skills"],
    profile_root="profiles",
)
```

`Runner(...)` 不会隐式读取 `config.yaml`。

runner workspace 默认是 `.dagent`，每次运行都会在 `.dagent/runs/<run_id>`
下记录自己的目录。在 ToolAgent 和 DagAgent message run 中，内置 file 和 shell
tool 的相对路径从当前 run workspace 解析。传入 `workspace=...` 时，dagent 会把
该目录本身作为 runtime workspace，因此默认 run workspace 会位于
`<workspace>/runs/<run_id>`。

如果应用已经拥有执行目录，可以给 `Runner.run(...)` 或 `Runner.stream(...)` 传入
`workspace_path=...`。dagent 会直接使用这个目录运行，不再创建 `<run_id>` 子目录。
这是运行时 workspace 选择能力，不是持久化能力；调用方仍然负责在 SDK 之外保存 run
state。继续一个 `RunState` 时，dagent 会复用 `RunState.workspace_path`。如果继续
state 的同时传入了不一致的 `workspace_path`，调用会报错。

## Provider 选项

`dagent.Provider` 面向 OpenAI-compatible chat completions endpoints：

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

使用 `reasoning` 传递常见 reasoning controls。只有当目标 endpoint 支持对应字段时，
才使用 `extra_request_args` 和 `extra_body` 传递 provider-specific 参数。

## 配置文件

当 provider settings、MCP servers、validation 或 profile directories 应从 YAML 加载时，
使用 `Runner.from_config(...)`：

```python
runner = dagent.Runner.from_config(
    "config.yaml",
    workspace=".dagent",
    capabilities=[search],
)
```

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
```

如果没有传入 path，`Runner.from_config(...)` 会解析 `DAGENT_CONFIG` 或
`./config.yaml`。相对的 `profiles.directory` 会从配置文件所在目录解析。

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

`python_tools` 条目只由本地 WebUI backend 加载。它不会让 `Runner.from_config(...)`
隐式 import 文件。`path` 条目指向 backend 进程可访问的 `.py` 文件，`names` 列出要注册的
导出对象。每个对象都必须由 `@dagent.tool` 创建，因此它是一个 `CapabilityBinding`，
并使用 `tool.<function_name>` capability id。WebUI 也支持上传 `.py` 文件；上传文件会复制到
`~/.dagent/python-tools/`，并在同一个用户配置文件中保存为 `source: "managed"` 条目。
`module` 条目会按名称 import 已安装或可导入的 Python module。`/python-tools/reload`
会 invalidate import caches，但不会 reload 已在 `sys.modules` 中的 module；需要类似
reload 的开发体验时，请使用 `path` 或上传后的 `managed` source。

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

MCP server name 是 dagent 的本地 workspace key，不是第三方 MCP tool 名。本地
`/mcp/servers` API 会强制这个 key 只能包含字母、数字和下划线，例如 `remote_docs`；
`mcp_servers` 和 `runner.add_mcp_server(...)` 也建议使用同样约定，让 id 保持可预测。
第三方 MCP tool 原始名称会保存在 capability config 中，并在 dagent 生成 `mcp.*`
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

MCP 需要可选依赖：

```bash
pip install "dagent-ai[mcp]"
```

MCP 注册是 all-or-nothing：如果 server 连接失败或任何已发现工具无法注册，runner 会回滚
这次注册尝试产生的 capabilities。

`runner.add_agent(...)` 会把一个叶子 `ToolAgent` 注册为 `agent.<name>`。已注册 agent
可以通过顶层 `ToolAgent`、`AutoAgent` 和 `DagAgent` 的 `agents` 字段暴露出来。
已注册的子 agent 不能再暴露其他子 agent。

## Capability 管理

WebUI backend 这类 host 可以管理 raw capability definitions：

```python
runner.register_capability(definition, handler, supports_context=False)
runner.replace_capability(definition, handler)
runner.set_capability_enabled("tool.search", False)
result = await runner.test_capability("tool.search", {"q": "dagent"})
runner.remove_capability("tool.search")

for definition in runner.list_capabilities(kind="mcp"):
    print(definition.id)
```

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
    workspace=".dagent",
    provider=provider,
    skill_roots=skill_roots,
    mcp_servers=mcp_servers,
    profile_root=profile_root,
)
```

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
runner = dagent.Runner(provider=provider, profile_root="profiles")
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
