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

也可以在构造后注册资源：

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

MCP server config 支持两种 transport。本地命令型 stdio server 可以省略
`transport`。Streamable HTTP server 使用 `transport: "http"` 和 `url`。
HTTP `headers` 的值会展开 host 环境中的 `${ENV_NAME}` 引用。

MCP 需要可选依赖：

```bash
pip install "dagent-ai[mcp]"
```

MCP 注册是 all-or-nothing：如果 server 连接失败或任何已发现工具无法注册，runner 会回滚
这次注册尝试产生的 capabilities。

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
运行时新增的模型条目属于 session state，除非 host 应用明确选择持久化它们。

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
