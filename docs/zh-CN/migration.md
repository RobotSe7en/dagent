# 迁移说明

dagent 已经发布公开 SDK contracts。本页记录升级时可能需要用户采取行动的面向用户变化。

## 当前发布线

当前包版本是 `0.6.0`。

## Unreleased

### 改变

- Capability definitions 现在把稳定 id 和调用名分开。`id` 仍是执行身份；
  `name` 是 LLM/PlanSpec 函数名；`display_name` 只用于 UI 展示。
- Runner.add_tools 现在是原子的：批量中的任一 binding 无法注册时，runner 会保持
  catalog 不变。重复注册完全相同的已有 binding 仍保持幂等。
- 本地 WebUI Python tool 条目使用 `source: "module"` 时，不再 reload 已存在于
  `sys.modules` 的 module。需要类似 reload 的开发体验时，请使用 `path` 或上传后的
  `managed` source。

### 破坏性改变

- `@dagent.tool` 仍不接收 `id=`。Python function tools 一律从函数名派生
  capability id，格式为 `tool.<function_name>`。`name=` 重新可用，但它只控制
  LLM/PlanSpec 函数名，不改变 capability id。
- `CapabilityDefinition.name` 和 `CapabilityDefinition.display_name` 是公开字段。
  省略时，`name` 默认为 capability id 把点替换为下划线，`display_name` 默认为
  `name`。
- Raw `CapabilityDefinition.id` 必须是以 `tool`、`agent`、`mcp`、`skill` 或
  `memory` 开头的 dotted capability id。每个 segment 只能包含字母、数字和下划线；
  至少需要两个 segment。
- LLM 可见的 PlanSpec 和 tool-call function names 现在使用
  `CapabilityDefinition.name`。如果设置了自定义 name，请同步更新已保存的 dynamic
  DAG PlanSpec 文本和 deterministic provider fixtures。

## 0.6.0

### 新增

- 单层子 agent 委派成为公开 SDK 能力。`Runner.add_agent(...)` 和
  `Runner.add_agents(...)` 会把叶子 `ToolAgent` 配置注册为 `agent.<name>`
  capabilities。
- 顶层 `ToolAgent`、`AutoAgent` 和 `DagAgent` run 可以通过
  `agents=["agent.<name>"]`、直接传入 `ToolAgent` 对象，或使用
  `agents="registered"` 暴露 runner 上注册的全部子 agent。
- Python 静态 `Dag` 节点可以直接 target `ToolAgent` 对象。本地 API/WebUI 管理的
  agent presets 会暴露为 `agent.<name>` capabilities，并把选择的 tools、MCP
  capabilities 和 skills 映射到公开 `ToolAgent` 字段。
- 新增 `examples/agent_delegation.py` 示例，演示如何注册叶子子 agent 并暴露给顶层
  run。

### 改变

- LLM 可见函数名现在统一由稳定 capability id 派生，把点替换为下划线。例如
  `tool.search` 会变成 `tool_search(...)`，`agent.helper` 会变成
  `agent_helper(...)`。
- Runner capability 注册现在会在 tools、MCP servers、raw capabilities 和 skill
  visibility 变化时，让已注册子 agent 的运行时 scope 保持同步。
- 本地 API agent preset payloads 现在使用公开 `ToolAgent` 字段名，并执行和 SDK
  相同的叶子子 agent 约束。

### 破坏性改变

- `@dagent.tool` 不再接收 `id=` 或 `name=`。Python function
  tools 一律从函数名派生 capability id，格式为 `tool.<function_name>`。需要改变公开
  id 时，请重命名函数，或用另一个函数名包一层实现。
- `CapabilityDefinition.name` 已删除。Raw capability definitions 现在只用 `id`
  作为稳定公开标识；LLM 可见函数名由该 id 派生。
- Raw `CapabilityDefinition.id` 必须使用受支持的 dotted capability id forms：
  `tool.<name>`、`agent.<name>`、`mcp.<server>.<tool>`、`skill.<name>` 或
  `memory.<name>`。每个 segment 只能包含字母、数字和下划线；首尾空白会被拒绝。
- LLM 可见的 PlanSpec 和 tool-call function names 现在由 capability id 派生，把点
  替换为下划线。例如使用 `tool_search(...)`、`tool_shell(...)` 和
  `agent_helper(...)`，不再使用 `search(...)`、`shell(...)` 或 `helper(...)`
  这类短名。请同步更新已保存的 dynamic DAG PlanSpec 文本和 deterministic provider
  fixtures。
- MCP capability ids 现在会对不符合 id segment 规则的原始 MCP server 或 tool 名称生成
  稳定 canonical key。例如 `mock-server` 不再映射为 `mock_server`；请查看已注册
  capability definitions，并更新已保存的 capability allowlists 或 DAG specs。
- 本地 API 管理的 profile 和 agent preset 名称现在只能包含字母、数字和下划线。创建新的
  managed profiles 或 agent presets 前，请把 dash 替换为 underscore。
- 本地 API MCP server name 是严格的 workspace key，现在只能包含字母、数字和下划线。
  这不限制第三方 MCP tool 原始名称；原始名称仍保存在 capability config 中。
- 本地 API agent preset JSON 现在使用 `ToolAgent` 字段名。请将 `capability_ids`
  改为 `capabilities`；已注册 preset 的 `agents` 必须为空，`review` 必须为
  `"fast"`。旧 preset 文件会作为错误报告，不会自动迁移。

## 0.5.2

- Breaking change：`Runner(...)` 和 `Runner.from_config(...)` 的默认 workspace
  现在是 `.dagent`，每次 run 默认记录在 `.dagent/runs/<run_id>` 下，除非显式设置
  `workspace` 或 `workspace_root`。
- Breaking change：内置 file 和 shell tools 现在会从当前 ToolAgent 或 DagAgent
  message run workspace 解析相对路径，而不是从 runner workspace root 解析。普通
  agent run 中的 `tool_write_file(path="notes.txt", ...)` 现在会写到
  `.dagent/runs/<run_id>/notes.txt`，不再写到 `.dagent/notes.txt`。如果代码需要把
  文件放在 runner workspace 下，请传入绝对路径，或把共享输入复制到每次运行的
  workspace 中。静态 DAG artifact path 仍使用已有 artifact 映射；内置 path-aware
  tools 继续传入 `artifact.path`。
- Breaking change：`Boundary` 现在只声明 `allowed_paths`。请从 SDK 代码、已保存的
  DAG specs 和 API payloads 中移除 `mode=` 与 `allowed_commands=`。Shell command
  safety checks 由内置 shell tool 负责，不再通过每个 node 的 `allowed_commands`
  配置。
- MCP 配置现在支持通过显式 `transport: "http"`、`url` 和可选 `headers` 接入
  Streamable HTTP servers。Header 值会在连接时展开 `${ENV_NAME}`。可选 MCP extra
  现在要求 `mcp>=1.27.1,<2`。

## 0.5.1

- 此 patch release 不需要迁移动作。它增加了 WebUI model provider 管理和 API key
  redaction 改进，不改变已文档化的公开 SDK contracts。

## 0.5.0

- Sandbox 执行现在会对不支持的 targets 和 capabilities fail closed。只有内置 tool
  capabilities 会在 `execution="sandbox"` 中执行。Python function tools、raw registered
  capabilities、MCP、skills、memory、agents、DAG、`DAGSpec` 和 `DagAgent` 在获得 sandbox
  支持前都必须使用 `execution="local"`；当 sandbox run 处于活动状态时，它们不再回退到
  host 执行。

## 0.4.2

- 内置 shell 命令 capability 现在是 `tool.shell`，DAG DSL 调用写作
  `tool_shell(command="...", cwd=".")`。升级前请把已保存的 `tool.run_command`
  capability ids 和 `run_command(...)` plan calls 改成新名字。不会注册旧名
  兼容别名。

## 公开 Surface 预期

以下行为视为已发布行为：

- package install name: `dagent-ai`
- Python import name: `dagent`
- [Python SDK 参考地图](python-sdk.md)中列出的 public exports
- Python tool capability ids 使用 `tool.<name>`
- MCP capability ids 使用 `mcp.<server>.<tool>`
- 显式 `Runner(...)` inputs
- 通过 `Runner.from_config(...)` 加载配置文件
- 静态 DAG 显式 dependency 要求
- 通过 `Runner.resume(...)` 进行 review-safe continuation

## Capability Ids

Python function tools 使用 `tool.<name>`。不要依赖旧的或内部的 capability id prefix 作为
兼容别名。

## Runner 配置

`Runner(...)` 使用显式 SDK inputs，不会隐式读取 `config.yaml`。加载 provider settings、
MCP servers、validation 或 profile directories 时，使用 `Runner.from_config(...)`。

## Profiles

内置 profiles 是 `dagent/resources/profiles/<name>.md` 下的打包 Markdown 资源。用户
profile directories 必须通过 `profile_root` 显式传入。

## 静态 DAG Dataflow

静态 DAG 要求显式 dependencies。像 `node.output.title` 这样的 value reference 不会创建
edge。请使用 `dag.add_edge(...)` 添加依赖。

## 未来条目

未来 release 改变已文档化的公开行为时，请添加：

- affected version
- old behavior
- new behavior
- migration steps
- related examples or docs
