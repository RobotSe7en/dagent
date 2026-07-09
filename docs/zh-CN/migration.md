# 迁移说明

dagent 已经发布公开 SDK contracts。本页记录升级时可能需要用户采取行动的面向用户变化。

## 当前发布线

当前包版本是 `0.6.8`。

## Unreleased

暂无未发布变更。

## 0.6.8

### 新增

- `dagent.capabilities.python_tools` 现在提供 SDK-owned helpers，用于从显式
  path、managed 或 module 条目加载配置化的 `@dagent.tool` Python sources。
- `Runner.reload_python_tool_sources(...)` 会加载配置化 Python tool sources，并返回稳定的
  registration result，不暴露可执行 bindings。
- `Runner.derive(...)` 可以基于显式 provider、workspace、MCP servers、Python tools、
  agents、profiles、sandbox 和 validation overlays 创建独立 runner。
- `Runner.mcp_server_snapshot(...)`、
  `Runner.list_mcp_server_snapshots()`、
  `Runner.reload_mcp_servers_with_snapshots(...)` 和
  `Runner.catalog_view(...)` 提供 runner-owned capability 与 MCP 注册状态的只读视图，
  不暴露 handler 或 catalog 内部对象。

### 改变

- 本地 API/WebUI backend 现在使用 SDK-owned Python tool loading helpers，不再维护单独的
  loader 实现。
- 架构约束现在明确禁止为了旧测试写兼容代码、重复实现、过宽兜底行为，以及把企业侧关注点放入
  installable SDK。

### 修复

- architecture boundary 测试不再因为 persistence 测试名里的过时 `tool_review` wording 失败。

### 破坏性改变

- 公共 Python SDK 无破坏性改变。

### 迁移步骤

- 现有 SDK 用户不需要改代码。
- 之前自行加载配置化 Python tools 或读取 MCP/catalog 内部状态的 host，应迁移到新的 `Runner`
  方法；RBAC、redaction、persistence 和 effective-configuration composition 仍保留在 host 层。

### 验证

- `uv run --extra dev pytest`
- `uv build`
- `git diff --check`

### 已知限制

- `Runner.catalog_view(...)` 是 runtime registration view，不是面向用户的授权视图。host
  在向用户返回 catalog data 前，仍需负责 RBAC、redaction 和 policy filtering。

## 0.6.7

### 新增

- 无。

### 改变

- Tool-agent 和动态 DAG 的 LLM 调用现在会在 provider 瞬态失败或请求超时时按递增等待重试，
  然后才进入原有失败路径。
- 默认 LLM 重试等待仍为 `1`、`2`、`5`、`10` 和 `30` 秒。

### 修复

- 永久性 LLM provider 失败，例如无效请求参数或不可重试的客户端错误，不再被重试。
- 流式 LLM 调用在已经输出 response token 后不再重试，避免重复输出部分 stream 内容。

### 破坏性改变

- 公共 Python SDK 无破坏性改变。

### 迁移步骤

- SDK 用户不需要迁移动作。

### 验证

- `uv run --extra dev pytest`
- `uv build`
- `git diff --check`

### 已知限制

- LLM 重试分类会保持保守。自定义 provider 需要抛出 timeout、connection error、
  可重试 status code，或 provider 特定的瞬态异常名，才会自动重试。

## 0.6.6

### 新增

- 本地 WebUI 现在可以在对应编辑开关启用时，通过 ONLYOFFICE 编辑项目文件和运行 artifact
  中的 Office 文档。
- 动态和静态编排 workspace 现在会持久化 run history，支持查看历史运行，并可删除已存储的
  run history 条目。
- Chat 和动态 DAG conversation 现在会持久化可见 message timeline，API 重启后可以直接恢复
  conversation。
- `examples/local_test_mcp.py` 提供本地 stdio MCP server，用于 registration 和工具调用
  timeout 诊断。

### 改变

- MCP 工具调用现在默认使用 `300` 秒超时，本地 WebUI MCP 表单也支持配置工具超时。
- 项目文件和运行 artifact 元数据现在包含文件 `version`，Office 预览可以在底层文件变化时刷新。

### 修复

- MCP 连接和工具调用超时现在会返回明确的 timeout 文案，不再是空错误文本。
- 动态、静态、独立和项目作用域流程中的编排 run history 行、hydration、run summary 和
  workspace 隔离都进一步收紧。
- interrupted chat 和持久化动态 DAG 历史 hydration 对已完成 trace 与可见 turn 的处理更稳定。

### 破坏性改变

- 公共 Python SDK 无破坏性改变。

### 迁移步骤

- SDK 用户不需要迁移动作。
- 如果 MCP 用户依赖之前较短的隐式工具调用截止时间，可以在 MCP server config 里显式设置
  `tool_timeout`。
- 如果现有本地 WebUI SQLite storage 来自不兼容的预发布 schema，可能会被重建。

### 验证

- `uv run --extra dev pytest`
- `npm --prefix web test`
- `npm --prefix web run build`

### 已知限制

- 当前本地存储后端是 SQLite 加本地文件系统 workspace。云端或多 worker 部署仍需要后续计划中的
  Postgres、对象存储和 worker execution 后端。

## 0.6.5

### 新增

- 本地 API/WebUI 的项目文件浏览器现在可以请求递归项目文件树，并为嵌套条目返回预览和下载
  元数据，同时跳过不安全的 workspace escape。
- WebUI 现在会汇总已完成 chat 的过程 timeline，使最终回答保持可见，同时仍可检查 reasoning、
  validation 和 capability 活动。

### 改变

- MCP server 注册和工具调用现在使用统一的显式默认 timeout：stdio 与 Streamable HTTP server 的
  `connect_timeout` 默认 `60` 秒，`tool_timeout` 默认 `90` 秒。
- WebUI 的项目 workspace、静态 DAG workspace、chat drafts、运行设置和已完成运行 trace
  展示经过打磨，导航更紧凑，布局更稳定。

### 修复

- 持久化 chat trace hydration 现在会通过与 live stream 相同的 dispatcher 回放已存储的
  stream envelopes，避免刷新后已完成 trace 缺失或状态不一致。
- 本地 API workspace file URI 现在使用平台感知的 file URI 处理，修复 Windows workspace
  路径。
- 项目文件树 listing 会跳过逃逸 workspace 的 symlink，并避免目录循环。

### 破坏性改变

- 公共 Python SDK 无破坏性改变。

### 迁移步骤

- SDK 用户不需要迁移动作。
- 如果 MCP 用户依赖之前较短的隐式工具调用截止时间，可以在 MCP server config 里显式设置
  `tool_timeout`。

### 验证

- `uv run --extra dev pytest`
- `npm --prefix web test`
- `npm --prefix web run build`

### 已知限制

- 当前本地存储后端是 SQLite 加本地文件系统 workspace。云端或多 worker 部署仍需要后续计划中的
  Postgres、对象存储和 worker execution 后端。

## 0.6.4

### 新增

- 本地 API/WebUI 现在会持久化动态和静态 DAG 编排 session，包括 draft DAG 状态、
  selected-node UI 状态、保存的静态 DAG 关联、运行事件历史和运行状态快照。
- 保存的静态 DAG 现在会保留 saved record 元数据、revision、project 归属、编辑器 layout，
  以及可跨 API 进程重启保留的 artifact uploads。
- 静态编排 run timeline 可以在运行完成后从持久化 run events 恢复。

### 改变

- 本地 API/WebUI store 现在按 kind 隔离普通 chat、动态 DAG、静态 DAG conversation；
  普通 chat stream 会拒绝编排 conversation。
- 静态编排 UI 在名称编辑、刷新和运行完成后，会保持保存 DAG 的显示名称和可见 revision
  状态稳定。
- WebUI 进入 chat workspace 时默认打开空会话，不再自动选中已有会话。有 artifact 时，
  artifact 面板仍可默认展开，但不会自动选中文件预览。
- 已删除非公开遗留本地 API route `/dags/{dag_id}/run/stream`。持久化静态 DAG run 使用
  `/saved-dags/{dag_id}/run/stream`。

### 修复

- 动态编排页面现在会把自己的历史和运行 workspace 与项目分离。项目范围的 DAG
  conversation 仍保留在智能工作台项目流程中。
- 静态编排运行完成后，运行事件和最终 timeline 不再消失。
- 静态编排 hydration 不再在保存或刷新 conversation 后，用保存的 DAG 状态覆盖当前编辑器和
  已完成运行结果。
- 并发触发静态 run 时，如果另一个请求刚创建了同一个 conversation session，不再静默丢失
  orchestration session 创建。

### 破坏性改变

- 公共 Python SDK 无破坏性改变。

### 迁移步骤

- SDK 用户不需要迁移动作。
- 检测到不兼容的未发布本地 SQLite API 旧库时会直接重建数据库，不做迁移。这不影响公开
  Python SDK。

### 验证

- `uv run --extra dev pytest`
- `npm --prefix web test`
- `npm --prefix web run build`

### 已知限制

- 当前本地存储后端是 SQLite 加本地文件系统 workspace。云端或多 worker 部署仍需要后续计划中的
  Postgres、对象存储和 worker execution 后端。

## 0.6.3

### 新增

- 本地 API 现在会在 API 存储层持久化项目、无项目会话、项目会话、运行状态快照、运行事件历史和
  review 记录；公共 SDK 不接触持久化，也不需要改动。
- WebUI 现在将无项目会话和项目分层展示。项目可以展开查看项目会话，并提供项目详情工作区，
  支持文件管理、目录浏览、上传、重命名、删除、下载、预览，以及文档配置。
- 持久化 chat stream 可以从存储的 `RunState` 快照恢复，包括 pending review、artifact
  manifest、trace，以及 API 进程重启后的项目会话状态。
- 无项目会话现在使用 `.dagent/projects/_conversations/<conversation_id>/workspace`
  下的持久 workspace；项目会话共享 `.dagent/projects/<project_id>/workspace` 下的项目
  workspace。

### 改变

- 持久化会话按 conversation 做单写者保护。API 使用带租约的会话锁，避免同一个会话被多个
  stream 并发驱动。
- 删除无项目会话会同时删除数据库记录和该会话根目录。删除项目会话会删除会话和运行记录，
  但保留共享的项目 workspace。
- 删除项目会在确认项目会话没有活跃 stream 后，同时删除项目数据库记录和本地项目根目录。
- WebUI 系统设置里的 OnlyOffice 配置已重命名为文档配置。

### 破坏性改变

- 公共 Python SDK 无破坏性改变。

### 迁移步骤

- SDK 用户不需要迁移动作。
- 已存在的本地一次性 runs、静态编排 runs、动态编排 runs 仍保留原有 run workspace 语义。
  新的持久化 chat conversations 会使用 `.dagent/projects/...` workspace 布局。
- 如果你测试过 0.6.3 的未发布开发分支，可以手动删除
  `.dagent/projects/_conversations` 下由早期版本残留的空目录。

### 验证

- `uv run --extra dev pytest`
- `npm --prefix web test`
- `npm --prefix web run build`

### 已知限制

- 静态 DAG runs 和动态编排工作区仍使用现有 run workspace 模型，除非显式传入 workspace root；
  它们尚未并入项目/会话持久化。
- 当前本地存储后端是 SQLite 加本地文件系统 workspace。云端或多 worker 部署仍需要后续计划中的
  Postgres、对象存储和 worker execution 后端。

## 0.6.2

### 新增

- WebUI 现在支持更多 artifact 类型的浏览器预览，包括 Office 文档和 PPTX artifacts。
- 本地 API 和 WebUI 暴露 OnlyOffice 配置；配置 OnlyOffice server 后，可以使用更丰富的
  文档预览能力。
- 通过 OnlyOffice 打开的 DOCX、XLSX 和 PPTX 现在可以分别为项目文件和运行产物开启
  edit mode。edit mode 会关闭 autosave，并且只有用户点击 Save 时才会写回原文件。
- 运行产物和项目文件元数据现在包含 `version` 字段；它由文件大小和纳秒级 mtime 生成，
  用于精确地让预览缓存失效。
- view-only 的 ONLYOFFICE 预览会在浏览器侧保留少量最近打开的编辑器实例，加快未变化
  文档之间的切换。
- 聊天 workbench uploads 现在可以附加到消息，并 materialize 到 run workspace，方便
  agent 检查用户提供的文件。
- WebUI 扩展了静态 DAG output binding 和 schema argument 编辑能力。
- WebUI 中 tools 和 MCP resources 现在使用更丰富的树形导航。

### 改变

- 优化了 artifact preview chrome、artifact tree 交互，以及折叠 artifact rail 的表现，
  便于更高密度地使用 workspace。
- Workbench upload 处理会更严格地校验文件名和 workspace 边界。
- OnlyOffice preview URLs 现在使用带签名的 file tokens，并在 token 中携带当前预览会话
  是否允许保存编辑。callback handler 只会在用户触发 force-save 时覆盖文件。

### 破坏性改变

- 无。

### 迁移步骤

- 此 patch release 不需要迁移动作。
- 如需使用 Office 文档预览或编辑，请在 WebUI 系统设置中配置 OnlyOffice document
  server。编辑能力默认关闭，需要显式开启项目文件或运行产物编辑开关。

### 验证

- `uv run --extra dev pytest`
- `source ~/.nvm/nvm.sh && npm --prefix web test`
- `source ~/.nvm/nvm.sh && npm --prefix web run build`

### 已知限制

- Office previews 需要外部 OnlyOffice document server。
- 编辑运行产物会修改 run workspace 中的文件，但不会重写已保存的 trace 历史。
- Workbench uploads 会 materialize 到本地 run workspace；如果运行在 ephemeral container
  storage 中，需要持久卷才能跨容器保留。

## 0.6.1

### 新增

- 本地 WebUI 现在会通过用户配置文件持久化用户管理的模型 providers、当前活动模型、
  用户 MCP servers，以及显式导入的 Python tool sources。
- WebUI 可以从本地路径或上传的 `.py` 文件导入 Python tools，并管理启用状态、验证、
  reload，以及删除上传后的 managed 文件。
- Python tool 导入对话框现在会识别带 `@tool` 或 `@dagent.tool` 装饰器的顶层函数，
  并自动填充函数名列表；用户仍然可以手动编辑该列表。
- WebUI 会在 MCP 服务视图中展示 MCP servers，但不再把 MCP capabilities 列入通用工具
  视图。

### 改变

- Capability definitions 现在把稳定 id 和调用名分开。`id` 仍是执行身份；
  `name` 是 LLM/PlanSpec 函数名；`display_name` 只用于 UI 展示。
- Runner.add_tools 现在是原子的：批量中的任一 binding 无法注册时，runner 会保持
  catalog 不变。重复注册完全相同的已有 binding 仍保持幂等。
- 本地 WebUI Python tool 条目使用 `source: "module"` 时，不再 reload 已存在于
  `sys.modules` 的 module。需要类似 reload 的开发体验时，请使用 `path` 或上传后的
  `managed` source。
- `/python-tools/reload` 现在只 reload 导入的 Python-tool capabilities。它不再重启整个
  runner，也不会重连无关 MCP servers；引用了已删除 Python tools 的 presets 会报告为
  agent errors。

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

### 迁移步骤

- 如果使用了自定义 capability `name`，请同步更新已保存的 dynamic DAG PlanSpec 文本和
  deterministic provider fixtures，让它们调用这些名字。
- 检查 raw capability definitions 和已保存的 allowlists，确认 dotted capability ids
  以 `tool`、`agent`、`mcp`、`skill` 或 `memory` 开头。
- 将 WebUI 管理的 profiles、agent presets 和用户 MCP server keys 中的 dash 或其他非
  字母、数字、下划线字符改名。
- 需要本地开发时类似 reload 的体验，请使用 `path` 或上传后的 `managed` Python tool
  sources；`module` sources 会复用已经 import 的 modules。

### 验证

- `uv run --extra dev pytest tests/test_api.py tests/test_python_tool_imports.py -q`
- `source ~/.nvm/nvm.sh && npm --prefix web test`
- `source ~/.nvm/nvm.sh && npm --prefix web run build`

### 已知限制

- Python tool 自动识别支持字面量 `@tool` 和 `@dagent.tool` 装饰器。如果源码通过 alias
  导入这些名称，请手动输入函数名。
- 无效的旧 WebUI agent preset 文件会被报告为错误，不会自动迁移。

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
