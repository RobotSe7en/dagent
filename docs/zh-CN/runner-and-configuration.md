# Runner 和配置

文件就绪通知与宿主增量保存方式，见[会话变化](conversation-changes.md)。

结果展示预算、类型化保留元信息及文件/搜索分页，见[工具结果恢复](tool-result-recovery.md)。

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
`runtime_directory` 支持绝对路径，也支持非空、无路径穿越的相对路径。自行管理存储或隔离边界的 host
应显式传入这两个值；如果运行数据只需驻留内存，也可以把 workspace 放在 tmpfs。
`Runner.from_config(...)` 使用相同的运行路径默认值，不会从 YAML 加载它们。

在 **Unreleased** SDK 中，同一会话的所有 run 共用一个 workspace：
`<workspace>/runs/conversation_<key>`，其中 `<key>` 是 `ConversationState.id` 的 SHA-256
前 32 位十六进制。续聊时传回上一轮的 `ConversationState`（保留其 `id`），才会继续使用该目录；
重建状态时换了 id，就会得到新目录。不传 `conversation` 的 run 会开启新会话，因此得到新目录。没有会话的
静态 DAG run 仍使用 `<workspace>/runs/<run_id>`。在 ToolAgent 和 DagAgent message run 中，
内置 file 和 shell tool 的相对路径从当前 run workspace 解析。SDK 私有数据隔离在调用方
选择的相对目录中：

- `<run-workspace>/<runtime_directory>/results` 保存超出展示上限的 tool/MCP 输出，以及大值和二进制产物；
- `<run-workspace>/<runtime_directory>/history` 保存压缩归档（压缩记录、源归档和旧版结果索引）。

这些目录按需创建。只构造 runner 或执行纯文本轮次不会创建它们。
只有读取结果的模型具备内置 `read_file` 工具时，展示内容才会溢出写入文件；否则展示
仍会截断，但不写文件。Shell 输出只在超过 shell 自身的内联上限（100,000 字节或 2,000 行）
时写入文件。溢出文件尽力写入，不带摘要，SDK 不校验、不回读，详见
[工具结果恢复](tool-result-recovery.md)。仅上传附件不会创建 history 目录。

SDK 不在 workspace 之间复制任何文件。同一会话的 run 共用 workspace，所以之后的轮次能读到
之前轮次写的文件，包括压缩归档。早期版本还会把压缩归档复制到
`<workspace>/<runtime_directory>/conversations`，并在每个新 run workspace 中恢复；该目录
现在不再读写，可以删除。

### 将 SDK 私有运行文件放到工作区外

传入绝对路径 `runtime_directory`，即可把 SDK 管理的结果和恢复资源放到模型工作目录之外：

```python
runner = dagent.Runner(
    workspace="agent-workspace",
    runtime_directory="/srv/dagent-runtime",
    provider=provider,
)
```

外置目录包含 `runs/<workspace-key>/{results,history}/`。
`workspace-key` 是解析后的 run workspace 路径的 SHA-256，不同工作区的结果文件互相隔离；
复用同一工作区时复用其存储命名空间。相对路径保留原有布局，`../runtime` 等相对路径穿越
仍被拒绝。绝对路径在构造 Runner 时解析。`from_config` 接受这个显式参数，`derive`
默认继承它，review checkpoint 会冻结它。

结果引用仍使用逻辑上的工作区相对路径 `.runtime/...`。内置 `read_file`、`grep` 和
`list_files` 在现有读取 boundary 检查后，映射到外置存储；文件工具不能写入这个逻辑命名空间。
SDK 不在工作区创建软链接或 `.runtime` 占位目录；工作区内 `.runtime` 下已有的软链接
不会改变运行资源引用到外置存储的映射。Shell 操作真实文件系统，不能使用这些
逻辑路径读取资源；请用文件工具检查保存的结果。宿主直接访问资源文件时使用：

```python
saved_path = runner.resolve_runtime_path(
    reference.path, workspace_path=result.workspace_path,
    runtime_directory=result.plan.runtime_directory,
)
```

多轮续聊应使用同一个绝对存储目录和同一个 workspace 路径，以便之前轮次的文件（包括
压缩归档）保持可用：它们留在所属 workspace 的命名空间中，在其他 workspace 续聊时保留原
路径，但读不到这些文件。改变设置不会自动搬迁已有文件；旧 checkpoint 按其冻结的设置续跑。
宿主仍负责保留存储文件，以及持久化 conversation/checkpoint 记录。

绝对目录只改变存储位置，不改变进程权限。本地 shell 仍拥有宿主进程的访问权限。
Docker 执行只为工具 worker 将 Runner 工作区和当前 run workspace 对应的
`runs/<workspace-key>/` 目录挂载为可读写，不挂载共享的 `runs/` 父目录；
沙箱启动会按需创建这些命名空间。需要防止删除时，仍应通过宿主权限和沙箱策略约束。

### 选择运行工作区

如果应用已经拥有执行目录，可以给 `Runner.run(...)` 或 `Runner.stream(...)` 传入
`workspace_path=...`。达智会直接使用这个目录运行，而不是 `workspace_root` 下该会话的目录。
这是运行时 workspace 选择能力，不是持久化能力；调用方仍然负责在 SDK 之外保存 run
的 conversation 和 review 状态。会话续聊会创建新的 run，也可以选择不同 workspace，
见[把会话移到另一个 workspace](conversation-changes.md#把会话移到另一个-workspace)。
review 续跑使用 `RunCheckpoint` 中冻结的 workspace；`Runner.resume(...)` 不接受替换
workspace。

同一会话同时执行的 run 共用其 workspace。SDK 不加锁；溢出文件和归档的文件名不会冲突，
但工具写的文件可能互相覆盖。workspace 按会话累积，是否删除由宿主决定。

对于由 profile 驱动的模型调用，SDK 还会在 system prompt 中动态加入
`Runtime Context` 段，写明解析后的 workspace root，并要求 agent 从该目录解析相对
文件路径。这适用于 tool agent、动态 DAG planner、注册到 DAG 的子 agent 和结果
validator。Profile Markdown 本身保持不变；runtime path 不会写入 profile 内容，也不会
对 profile 做模板替换。`FeedbackLearnerAgent` 等底层 profile-backed helper 在调用时
收到 `workspace_path` 后，也会使用相同的动态段。

模型会在该 system 段中收到解析后的 run workspace。附件记录和外置结果使用 workspace
相对路径。附件的字节数描述上传时的内容；早期版本的附件还保留上传时的摘要。工具输出引用只说明溢出文件写在哪里，不带摘要，文件也不受校验，
之后读取到的是文件当时的内容。

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

### 编辑上传文件

向 message run 传入
`input_uploads=[dagent.ArtifactUpload(filename="note.txt", content=b"draft")]`，
即可在工作区写入 `uploads/note.txt`。会话上传和静态 DAG artifact 上传只写入一次；写入
字节数不足时，会在 agent 执行前抛出 `OSError`。在 **Unreleased** SDK 中，写入后不再回读，
新的 `Attachment` 记录不带摘要（`sha256` 为 `None`），只记录字节数。

上传成功后，工具可以修改、覆盖或删除工作文件。SDK 在等待审核、完成运行或续聊时
不再核对历史附件的元数据。`Attachment` 保留上传时元数据，不保证文件仍存在或
仍具有原始内容。工具读取已删除文件时，按正常文件访问错误处理。同一会话的 run 共用
workspace，之后上传同名文件会覆盖之前的文件，之前的附件记录描述的内容就不存在了。

继续编辑时，向下一轮传入上一轮的 `conversation`，它会使用同一个 workspace；自行选择
workspace 时，传入相同的 `workspace_path`。如选择其他工作区，由调用方复制需要的工作文件；
SDK 不复制历史上传文件、溢出文件或压缩归档，不恢复已删除附件，也不改写路径。需要原始
版本时，由宿主使用独立快照保存。

可离线运行[上传文件编辑示例](../../examples/editable_uploads.py)，查看在同一显式工作区
内上传、编辑和续聊的完整流程。

## Provider 选项

`dagent.Provider` 面向私有 vLLM 部署，同时支持 OpenAI Chat Completions 和
Responses：

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

构造 provider 不会访问网络。第一次模型调用、精确 token 计数，或显式调用
`await provider.inspect_capabilities()` 时，provider 会读取 vLLM 的
`/openapi.json` 与 `/version`，并缓存类型化能力报告。`protocol="auto"` 只有在 Responses
满足当前请求所需的 tools、streaming、structured output 与 reasoning 能力时才优先使用
它；否则在 Chat 满足请求时选择 Chat。探测没有结论时会发出 warning 并使用 Chat。
显式协议不会 fallback；一次 POST 失败后也绝不会换协议重试。

两个协议都按无状态方式使用：每次调用发送本次选定的完整上下文。Responses 始终设置
`store=False`，不使用 `previous_response_id` 或加密 reasoning content。
`reasoning_effort` 在 Chat 中映射为 `reasoning_effort`，在 Responses 中映射为
`reasoning.effort`。SDK 不支持 token 数形式的 reasoning budget；旧的嵌套
`reasoning` 对象会被拒绝。

可选的 `reasoning_mapping` 配置（自 0.9.19 起）分别适配 Chat 和 Responses 的请求字段路径
及字符串强度名称。未配置映射的协议保留内置行为，详见
[自定义思考字段与强度名称](model-context-and-reasoning.md#自定义思考字段与强度名称)。

`chat_reasoning_field` 只控制 Chat 的 reasoning 回放序列化。`auto` 对已识别的 DeepSeek
官方 V4 端点与模型使用 `reasoning_content`，对 vLLM 使用 `reasoning`，其他情况省略。
显式 `reasoning_content`、`reasoning`、`omit` 优先。仅凭模型名不能确认代理端点的协议，
请按其文档显式配置字段。响应解析会接受常见的两种输出字段名，并统一保存到
`AssistantMessage.reasoning`。

`token_counting="auto"` 会在 vLLM 声明能力时使用 `/tokenize`，请求包含 messages
和 tool schemas。精确计数不可用时发出 warning，并使用确定性启发式估算；`"vllm"`
会让 tokenize 失败显式报错，`"heuristic"` 则不为 token 计数做探测。探测得到的
`max_model_len` 是默认总 context limit；显式 `context_window_tokens` 会覆盖自动值，但
成功探测后若显式值更大则拒绝。没有显式值且探测失败时 warning 并 fallback 到 131,072（128K）。
DeepSeek 官网 V4 模型通过端点与模型 ID 识别其公布的 1M 窗口，启发式计数时同样生效；
详见[模型上下文](model-context-and-reasoning.md#token-计数与压缩)。
`max_output_tokens` 可选；配置后既从输入容量中扣除，也会作为真实生成上限发送。未配置时
不发送输出限制字段。

流式 usage metadata 默认关闭，因为部分兼容 endpoint 会拒绝 `stream_options`。
只有目标 endpoint 支持 OpenAI 的流式 usage 扩展时才设置
`stream_include_usage=True`；SDK 结果中的 usage 始终是可选值。

自 0.10.0 起，`capture_cache_usage=True` 选项将标准服务端缓存计数记录到
`ModelTokenUsage.cached_input_tokens`。它只改变响应解析，不会自动开启流式 usage
或服务端缓存。兼容的 Chat 流式端点仍需单独设置 `stream_include_usage=True`。
详见[提示缓存用量](model-context-and-reasoning.md#提示缓存用量)。

`reasoning_capture="field_and_tags"` 会记录专用 reasoning 字段和 tag 内容；
`reasoning_capture="field"` 只信任专用字段并丢弃 tag 内容；tag 不会残留在可见正文中。
capture 只控制响应解析，不会在请求中启用 reasoning。

对于 structured planner call，达智会在本地校验返回对象，并把完整 schema 映射到 Chat
的 `response_format.type="json_schema"` 或 Responses 的
`text.format.type="json_schema"`。

reasoning 回放模式、直观 wire 示例、压缩顺序和能力探测细节见
[模型上下文与推理](model-context-and-reasoning.md)。

`timeout_seconds` 控制 provider request timeout。Tool-agent、动态 DAG 的 planning/replanning、
路由、结果验证和上下文压缩的 LLM 调用在遇到瞬态请求失败时最多重试 5 次，重试前分别等待
`1`、`2`、`5`、`10`、`30` 秒。
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
`config.yaml` provider 仍是默认模型。用户模型条目也支持
[`reasoning_mapping`](model-context-and-reasoning.md)（自 0.9.19 起）；WebUI 会将其传给 provider，
并在选择或保存模型时保留。WebUI 会同时注册项目和用户 MCP servers。同名冲突时，
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

派生 runner 默认继承 `runtime_directory`；派生 runtime 需要另一套私有布局时，
可显式传入不同的安全相对路径或绝对存储目录。

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

Result validation 运行在包含 execution context 的 tool 和 DAG outcomes 上。
Tool run 耗尽步数后仍可接收验证反馈，通过重试补全最终答案；终止模型错误会跳过验证。
纯 chat-only responses 不会被 validation。

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

## 可选上下文观测

自 0.10.1 起，向 `Runner` 或 `Runner.from_config` 传入
`context_observability=True`，通过 `stream` / `resume_stream` 观测 ToolAgent
HTTP 调用及 cache-aware 上下文维护。默认 `False`；此开关不改变生成请求、策略、
检查点或 SDK 持久化状态；开启后 legacy 摘要使用内部流式调用报告真实进度。见[流式观测](results-streaming-review.md#上下文与请求观测)。

## 限制已结束根 run 的保留状态

`Runner(max_retained_runs=None)` 默认保留全部已结束 run。设为 `0`，会在根 run 或
resume 完整收尾后释放其重状态；正整数只保留最近结束的若干根 run。运行中和等待
审批的 run 不计数、不淘汰。较早开始的 run 经审批后结束，仍算最近结束。
正常和异常退出都在交付及失败状态完成处理后清理。

清理包括 Runner 保存的 `RunState`、checkpoint、失效审批索引，以及它创建的 DAG
子 agent 会话缓存。外部 provider 自行管理其缓存。workspace、结果文件和压缩归档
仍可使用；调用者已取得的结果与导出 checkpoint 不受影响。恢复始终显式提供
checkpoint，不依赖进程缓存。

`Runner.from_config(...)` 也可显式传 `max_retained_runs`，该进程策略不从 YAML
加载，不进入执行指纹。`runner.derive()` 继承上限但不继承历史；
`runner.derive(max_retained_runs=None)` 显式恢复无限保留。

上限限制已结束 run 的重状态数量，不承诺进程内存恒定。设置上限时，已用 run id 和
已消费审批 id 防重放元数据随历史增长，淘汰后仍不能复用 `run_id`。无限保留时，已保存
的状态本身即阻止复用，因此尚未保存任何状态就结束的 run 可用同一 `run_id` 重试；
持久化审核状态在 ACK 后释放，其 id 仍视为已用。调用者持有的结果、事件队列、
provider/宿主日志和单次 run 的 `new_items` 也可能占用内存。参见
[离线示例](../../examples/run_retention.py)。

ToolLoop 持久化流的审核 checkpoint 由宿主持有：审核最终批次 ACK 后立即释放本地重状态，
不受普通保留上限影响。普通持久化终态不缓存 checkpoint。宿主持有历史时配置
`max_retained_runs=0`。参见[持久化流](conversation-changes.md)。
