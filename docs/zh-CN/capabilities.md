# Capabilities

Capabilities 是注册到 `Runner` 的可执行动作。Agents 和 DAG nodes 不直接执行函数；
它们通过 runtime capability catalog 调用 capability ids。

## Capability Ids

| 来源 | 常见 id 形式 |
| --- | --- |
| Python function tools | `tool.<name>` |
| MCP tools | `mcp.<server>.<tool>` |
| 内置 skill accessors | `skill.list`, `skill.view` |
| Memory accessors | `memory.write`, `memory.search` |
| 已注册 subagents | `agent.<name>` |

Capability ids 是公开行为。不要依赖这里未记录的 legacy aliases。

Raw `CapabilityDefinition.id` 必须以受支持的 kind 前缀开头（`tool`、`mcp`、
`agent`、`skill`、`memory`），并且至少包含两个 dotted segments。每个 segment
只能包含字母、数字和下划线；首尾空白会被拒绝。上表列出的是 dagent 默认使用的
常见形式，不表示所有自定义 capability 都必须固定为同样的 segment 数量。

## 内置工具

每个 `Runner` 都会注册一组默认工具。所有路径参数在 handler 执行前都会经过节点
boundary 检查。Tool-agent run 中，如果一次 capability call 会跨越 boundary，运行会先暂停
等待人工 review；批准只对这一次 capability call 生效。DAG review 的授权范围更大：
批准某个 DAG 版本表示允许其中已审核的节点按展示的 boundary 执行。该授权来自 DAG
review resume 流程；静态 DAG 和 fast no-review 的 DAG revision 仍会执行节点 boundary
检查，并在越界时 fail closed。

| 工具 | 风险 | 行为 |
| --- | --- | --- |
| `tool.read_file` | low | 读取 UTF-8 文本文件。可选 `offset`（1 起始）和 `limit` 分页读取大文件；单次读取上限 2000 行 / 200 KB，超限时末尾追加标明已读范围的 `[TRUNCATED]` 行。二进制文件直接报错。未截断的完整读取按原文逐字节返回。 |
| `tool.write_file` | medium | 写入 UTF-8 文本并自动创建父目录。新文件遵循进程 umask；覆盖已有文件时保留原文件权限；替换写入会让目标路径与同 inode 的其他硬链接断开。返回写入字节数。 |
| `tool.edit_file` | medium | 将 `old_string` 的唯一一次精确匹配替换为 `new_string`。匹配必须唯一，并且在 UTF-8 解码后逐字精确匹配：零匹配或多处匹配都会失败，并提示先读文件、补充上下文。保留既有换行与 UTF-8 BOM；结果附带一段简短 unified diff。 |
| `tool.list_files` | low | 列出路径下的文件与目录（目录以 `/` 结尾），最多 `depth` 层（默认 3）。传入 `glob`（如 `*.py`）时只列匹配的文件。输出达到 500 条后停止；结构化返回值就是已展示条目列表，DAG map 节点可直接对其扇出。 |
| `tool.grep` | low | 使用 Python 正则语法搜索文件，可选 `glob` 文件名过滤。`PATH` 上有 `rg` 时使用兼容参数委托 ripgrep（argv 调用，绝不经过 shell），否则回退纯 Python 扫描。两种后端都不应用项目 ignore 文件，但都会排除内置的重型目录。输出为 `file:line:content`，上限 200 条。 |
| `tool.shell` | high | 在受限工作目录内执行 shell 命令，默认 30s 超时。危险模式被硬性拦截，工作目录必须存在，显式 shell 路径参数会经过 boundary 检查，超长输出保留尾部（200 行 / 100 KB）并加 `[TRUNCATED]` 头。 |

每个 capability 有三个名字。`id` 是稳定执行身份，用于 scopes、traces、reviews 和
DAG invocation payloads。`name` 是 LLM 可见函数名，用于 provider tool calls 和
PlanSpec DSL。`display_name` 只用于 UI 展示。省略 `name` 时，dagent 默认把 capability
id 中的点替换为下划线；省略 `display_name` 时，默认等于 `name`。

`tool_read_file` 的输出不带行号前缀，从读取结果中复制的文本可以原样作为
`tool_edit_file` 的 `old_string`。推荐的编辑流程：先读文件，复制要修改的原文，
再用足够的上下文调用 `tool_edit_file` 使匹配唯一。

## Sandbox 执行

`execution="sandbox"` 目前只支持上面列出的内置 tool capabilities。它们会先在 host
上执行 boundary 检查，然后把检查后的工具调用路由到当前 sandbox session。

通过 `@dagent.tool` 或 `Runner.register_capability(...)` 注册的 Python function tools、
MCP tools、skill capabilities、memory capabilities、agent capabilities、DAG、`DAGSpec`
和 `DagAgent` 目前还不能在 sandbox 中执行。它们在 `execution="sandbox"` 下会 fail
closed，而不会回退到 host 执行。对这些 capabilities 请使用 `execution="local"`。

`Runner.test_capability(..., execution="sandbox")` 使用 runner workspace 作为 sandbox
workspace。默认 runner workspace 是 `.dagent`；`Runner(workspace=...)` 下已有的文件
对受支持的内置工具可见。

## Python Function Tools

用 `@dagent.tool` 装饰 Python 函数。参数注解会生成 tool input JSON schema；返回注解会
生成 output schema。Python 函数名仍决定 capability id：`search` 会注册
`tool.search`。传入 `name=` 可以选择 LLM/PlanSpec 函数名，传入 `display_name=` 可以选择
UI 文案。decorator 不接收 `id=`。

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

可以在构造时或之后注册 tools：

```python
runner = dagent.Runner(provider=provider, capabilities=[search])
runner.add_tool(search)
```

`runner.add_tools([...])` 用于原子批量注册。拥有一组配置化 Python function tools 的
runtime manager 可以使用 `runner.reload_tools(groups, replace_ids=...)` 删除之前归它管理的
ids、按 group 独立注册当前 tools，并收集 group 或已注册 agent 的错误，而不是把旧 id 缺失
当成用户删除单个 capability。`replace_ids` 只能指向非内置的 `tool.*` capabilities；
MCP tools、agent capabilities 和内置 tools 必须通过各自的生命周期 API 管理。

持久化用户配置 Python tool sources 的 host 可以通过 SDK 加载显式的
`UserPythonToolConfig` 条目，而不用自己 import 文件：

```python
from pathlib import Path


result = runner.reload_python_tool_sources(
    configs,
    user_config_dir=Path("~/.dagent").expanduser(),
    managed_root=Path("~/.dagent/python-tools").expanduser(),
    replace_ids=previous_python_tool_ids,
)

print(result.capability_ids_by_source)
print(result.errors)
```

预览和校验流程可以从 `dagent.capabilities.python_tools` import
`discover_python_tool_names`、`load_python_tool_sources` 或
`read_python_tool_source`。这些 helpers 可以加载 `path`、`managed` 和 `module`
sources；它们只加载显式列出的 `names`，校验每个导出对象都是由 `@dagent.tool`
生成的 `CapabilityBinding`，并按 source 返回错误，不会隐式扫描目录。source 读取和
decorator name discovery 是基于文件的，不读取已安装 module。

Agents 声明自己能使用什么：

```python
agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["tool.search"],
)
```

如果需要不同的公开 capability id，请重命名 Python 函数。

## 结构化结果

普通 `str`、`dict`、`list`、数字、布尔值、tuple、bytes 和 Pydantic models 会被转换成
`CapabilityResult.content` 和 `CapabilityResult.value`。静态 DAG 节点输出引用默认从
`value` 读取。

如果工具直接返回 `CapabilityResult`，且 completed result 没有显式 `value`，则使用
`content` 作为 value。

## Tool Context 和 Boundaries

需要 run workspace 或 callbacks 的工具可以选择接收 runtime context：

```python
from pathlib import Path

import dagent


@dagent.tool(risk="medium", supports_context=True)
def write_note(path: str, content: str, *, context, callbacks=None) -> str:
    run_workspace = Path(context.workspace_path).resolve()
    resolved = Path(path).resolve()
    resolved.relative_to(run_workspace)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"wrote:{path}"
```

DAG nodes 可以为有副作用的工作传入 boundaries：

```python
report = dag.artifact("report", "outputs/report.md")

write_node = dagent.Node(
    "write_report",
    target=write_note,
    inputs={"path": report.absolute_path, "content": search_node.output},
    artifact_outputs=[report],
    boundary=dagent.Boundary(
        allowed_paths=[report.absolute_path.as_expr()],
    ),
)
```

Boundary 声明节点可以读取或写入的路径。Boundary values 可以是字面量字符串，也可以是
结构化 value references。

## Capability Policies

`CapabilityPolicy` 记录 risk 和执行要求：

```python
policy = dagent.CapabilityPolicy(
    risk="medium",
    requires_review=True,
    network=False,
    secrets=[],
)
```

Agents 和 runs 上的 review 设置决定 medium/high-risk 工作什么时候暂停等待批准。
Boundary review 独立于 risk review：如果 tool-agent 调用试图读写 boundary 之外的路径，
run 会以 `payload.reason == "boundary_violation"` 暂停。批准该 review 只会执行同一
次调用一次，不会扩大整个 run 的 boundary；拒绝则把 denial 消息反馈给 agent。硬性拦截的
shell 危险模式（例如破坏性系统命令）不可通过 review 放行。

## MCP Tools

MCP stdio 和 Streamable HTTP server tools 在 server 注册后会变成普通
`mcp.<server>.<tool>` capabilities：

这里的 `<server>` 和 `<tool>` segment 是 dagent 的公开 key。原始 MCP server 和 tool
名称会保存在 capability `config` 中；不安全的原始名称会通过稳定短 hash canonicalize，
避免不同外部名称在 normalize 后发生碰撞。第三方工具的 id 请通过
`runner.add_mcp_server(...)` 返回值或 `/capabilities` 查看，不要手写猜测。

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

远程 Streamable HTTP server 使用显式 HTTP transport 注册：

```python
runner.add_mcp_server(
    "remote_docs",
    {
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
    },
)
```

注册 MCP servers 前先安装 MCP extra：

```bash
pip install "dagent-ai[mcp]"
```

动态 MCP 注册和替换见 [Runner 和配置](runner-and-configuration.md)。

注册完成后，`runner.mcp_server_snapshot(name)` 和
`runner.list_mcp_server_snapshots()` 会返回只读 MCP identity snapshots，包含
capability ids、原始 server/tool names，以及公开 capability definitions。批量 host
reload 可以使用 `runner.reload_mcp_servers_with_snapshots(...)`；它使用和
`runner.reload_mcp_servers(...)` 相同的批量 reload 语义，同时返回包含成功 snapshots 和逐
server errors 的 `MCPServerRegistrationResult`。持久化已发现工具时应使用这些 SDK 结果，
而不是在 SDK 外部重建 `mcp.<server>.<tool>` ids。

已经信任保存下来的 `MCPServerSnapshot` 的 host，可以用 `lazy_connect=True` 传回：

```python
runner.add_mcp_server(
    "remote_docs",
    config,
    snapshot=snapshot,
    lazy_connect=True,
)
```

这会先注册 snapshot 中的 capability definitions，而不会立即连接 server。SDK 会在首次
tool call 时连接对应 MCP server。Snapshot 只用于注册和校验元数据；可执行行为仍来自配置的
MCP server。Lazy registration 必须提供 snapshot，并且当前 server configuration 仍控制
`enabled`、`include_tools`、`exclude_tools`、`risk` 和 network policy。

## Runtime Contract 边界

Runtime contracts 是给宿主进程使用的进程边界契约，前提是宿主已经准备好 workspace
和凭证。它们不包含用户、组织、项目、RBAC、授权过滤、持久化、队列领取、租约、
限流、审计、用量、计费、provider key 代理、Docker 生命周期或 worker 编排。

## 直接测试 Capability

使用 `Runner.test_capability(...)` 单独执行一个 capability 进行检查：

```python
result = await runner.test_capability("tool.search", {"q": "dagent"})
print(result.status)
print(result.value)
```
