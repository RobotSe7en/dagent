# 迁移说明

达智已经发布公开 SDK contracts。本页记录升级时可能需要用户采取行动的面向用户变化。

## 当前发布线

当前包版本是 `0.9.5`。

## Unreleased

## 0.9.5

### 变更：可观察的 DAG 设计 provider 流

- `Runner.design_dag(...)` 新增可选的同步 `on_event` callback。传入时会消费 provider 的
  真实流，并通过现有 `RunStreamEvent` 报告 response 和 validation 事件。结构化候选 JSON
  不会作为 content delta 暴露，等待返回的 `DAGDesignResult` 仍是唯一终态值。不传 callback
  时保留 0.9.4 的 `provider.chat` transport 行为。
- 省略 `agent` 时，设计现在使用专用内置 `dag_design` profile，不再使用面向执行的
  `dag_agent` profile。如需保留旧 prompt，可传
  `DagAgent(planner_profile="dag_agent")`；也可传显式自定义 profile。
- 返回的设计 conversation 现在把自然 summary、answer 或确定性失败说明保存为可见
  assistant content，不再保存 provider 原始结构化 JSON。不需要 schema 或持久化迁移；
  已有 conversation 值仍可作为合法输入。
- 设计专用 `dag_design` profile 仍可由 `Runner.design_dag(...)` 加载，但内置 host 不会
  将它作为可执行的 `agent.dag_design` capability 发布或接受。

## 0.9.4

### 新增：中立的非执行型 DAG 设计

- `Runner.design_dag(...)` 现在可以根据自然语言创建、修改、检查或解释完整的类型化
  `DAGSpec` 候选。带 tag 的结果变体包括 `DAGDesignProposal`、`DAGDesignNoChange`、
  `DAGDesignAnswer` 和 `DAGDesignFailure`。
- `DAGDesignSelection` 携带可选的选中 node ids。每种结果都包含可继续使用的
  `ConversationState`、provider 可用时的 `ModelTokenUsage`、请求 `ContextUsage`，以及
  结构化 `DAGDiagnostic`。
- `inspect_dag_spec(...)` 提供确定性 diagnostics，同时保持现有
  `validate_dag_spec(...)` 异常 contract 不变。

### 行为和兼容性

- 设计调用使用正常的 Runner catalog 和可选 `DagAgent` scope。候选中的 capability kind、
  risk 和 boundary 始终由 catalog 值覆盖；未知、disabled 或超出 scope 的 id 会 fail closed。
- 设计调用只调用 chat provider。它不会创建 Run、review、checkpoint、workspace artifact
  或 capability result，也绝不会调用 capability handler。现有 `Runner.run(...)` 行为不变。
- 修改会返回完整候选；语义未变时保留稳定的 node 和 invocation id，并保持未改边的字段与
  顺序。`DAGEdge` 不增加 id 或 layout 字段。
- 不需要数据或配置迁移。持久化、revision、semantic diff、部分采纳、layout、policy 和
  audit 仍由 host 负责。

### 变更：已批准越界路径可在同一 run 内复用

- 可审核的 boundary violation 现在包含规范化后的 `payload.boundary_paths`。批准后只会授权
  这些已报告路径供同一 run 的后续 tool-agent 调用使用，因此 invocation id 改变不会让同一路径
  重复审核。
- 不同路径仍需审核；该授权不会成为 user、project 或跨 run 策略，硬性 boundary 拦截仍不可放行。
- `RunState` 和 `RunCheckpoint` schema version 不变。新 checkpoint 通过既有 invocation
  boundary 携带授权；不含 `boundary_paths` 的旧 checkpoint 继续保持单 invocation 批准语义。

### 变更：不绑定项目的静态编排运行

- 内置 API/WebUI 现在把已保存静态 DAG 视为可复用定义，而不是 project 或 conversation
  资源。每次调用 `POST /saved-dags/{dag_id}/run/stream` 都会创建新的 run id 和隔离的
  `.dagent/projects/_runs/<run_id>/workspace`。
- 新静态 run 不获取 conversation lock；run 记录中的 `project_id`、`conversation_id` 均为
  null，历史仍按 `saved_dag_id` 分组。
- 归档 saved DAG 会删除其分组运行历史和隔离的 run workspace；存在 queued/running run
  时返回 `409`，需等待运行结束。
- 静态 review 通过 `POST /reviews/{review_id}/resume` 恢复，使用已持久化的
  `RunCheckpoint` 和该 run 的专属 workspace；API 或 `Runner` 重启后语义保持确定。

### 本地 API 与存储兼容性

- `POST /saved-dags` 不再接受 `project_id`，saved-DAG 响应也不再包含该字段；
  `GET /saved-dags` 不再按项目过滤。
- `POST /saved-dags/{dag_id}/run/stream` 不再接受 `project_id` 或 `conversation_id`，只发送
  可选的 `graph_input`。
- SQLite migration version 2 会把旧 `saved_dags.project_id` 列设为 null，从而解除已有定义
  的项目绑定；新数据库不再创建该列。删除项目也不再删除 saved DAG 定义或其上传输入。
- 已存在的、基于 conversation 的静态 run 和 pending review 仍可读取，并可通过原来的
  conversation/project 路由恢复。该兼容路径只服务已持久化 run，不用于新静态 run。
- 这些是内置本地 API/WebUI contract 的变化；公开 Python SDK、`DAGSpec`、`RunState` 和
  `RunCheckpoint` schema/version 均不受本次修改影响。

### 迁移步骤

- 从 saved-DAG 创建请求中移除 `project_id`。
- 从 saved-DAG 运行请求中移除 `project_id` 和 `conversation_id`；通过
  `GET /saved-dags/{dag_id}/runs` 获取该定义的运行历史，而不是按 conversation 分组。
- 新静态 run 的 review 使用无项目 review endpoint 恢复；不要为新运行创建静态
  conversation 或 orchestration session。

## 0.9.3

### 新增

- 静态 DAG 输入 artifact 现在暴露 `artifact.files`：它是从当前 run 的
  `artifact_uploads` 物化出的、确定顺序且 JSON-safe 的 `ArtifactFileRef` 列表。条目带有
  相对 workspace 的 `path`、`name`、`size` 和调用方可选提供的 `media_type`。
- `ArtifactFileRef` 和 `ArtifactFileManifest` 成为公开 SDK contract。
  `MapNode(over=artifact.files, ...)` 可直接通过 `dagent.item` 处理当前文件条目。

### 行为和兼容性

- 该列表是上传时快照，不是 workspace 扫描。空的可选输入解析为 `[]`；checkpoint resume
  时，之后写入的文件或其他 run 的文件不会出现。
- 物化会拒绝不安全文件名、目录穿越、重复目标、经 symlink 的上传路径、超过 256 个文件、
  单文件超过 25 MiB 或总量超过 100 MiB。现有 `artifact.path`、`artifact.paths` 和绝对
  artifact expression 语义不变。

### Checkpoint 版本与兼容性

- 新 Run 生成 `RunState` V4，以及 `ResolvedRunPlan` / `RunCheckpoint` V5，以持久化权威的
  输入文件快照。
- 包含 V3 `RunState` 的既有 V4 checkpoint 仍可恢复。它们显式拥有空输入文件清单；resume
  不会扫描 workspace，也不会从 workspace 推断文件。一次成功恢复后的结果会生成新的 V5
  checkpoint。
- 其它版本组合仍然无效。特别是 V3 state 不能声称包含输入文件清单，SDK 也不会为旧
  checkpoint 透明地制造清单。

### 迁移步骤

- Host 可以继续恢复已持久化的 V4 checkpoint。成功 resume 后应持久化返回的 V5 checkpoint；
  所有新 Run 均使用 V5。
- 对文件输入，声明目录 artifact（例如 `inputs/workflow_input_files/`），并调用
  `Runner.run(..., artifact_uploads={artifact_id: [ArtifactUpload(...)]})`。消费
  `artifact.files`、如 `artifact.files[0].path` 的条目字段，或对列表使用 map。不要扫描
  run workspace 来重建该 contract。

### 验证与已知限制

- `uv run --extra dev pytest tests/test_artifact_file_manifests.py`
- `uv run --extra dev pytest`
- `artifact.files` 适用于接受 `artifact_uploads` 的静态 `Dag` 和 `DAGSpec` run；它不会新增
  workflow-start protocol，也不会改变 `StartNodePayload`。

## 0.9.2

### 新增

- 静态 DAG 中顶层、直接的 `Node(..., target=ToolAgent(...))` 现在会对其内部工具调用复用
  既有的 `ToolAgent` 审核和恢复流程。`review="careful"` 会让中、高风险内部工具暂停等待
  审核；任何策略下，boundary 越界都可以暂停，以请求仅针对该 invocation 的 boundary override。
- 静态 DAG Agent 审核 checkpoint 会持久化挂起的节点 invocation 和内部 tool-agent state，
  可由兼容的新 `Runner` 恢复。直接 Agent 的配置会写入 fingerprint，因此 profile、限制、
  策略、skill scope 或内部工具范围变更时，不会悄然改变恢复后的运行语义。
- WebUI 现在会显示这类静态 DAG 审核，在作出决定前保持 run 为 `awaiting_review`，并可在没有
  独立 conversation-state record 的情况下恢复已保存的静态 DAG。
- `dev` extra 现在包含 `pip`，所以 `uv sync --extra dev` 和 `uv run --extra dev` 会为开发流程
  提供 `python -m pip`。

### 行为与兼容性

- 普通静态 capability node（包括高风险 node）仍由 DAG 作者直接授权，不会新增审核弹窗。
- 只支持顶层直接 Agent capability node。`MapNode`、`Subgraph` 或 `LoopNode` 内的 Agent，
  以及会暴露另一 Agent 的已注册 Agent，都会在执行前被拒绝，因为其嵌套进度尚不能安全恢复。
- 批准和拒绝会继续同一个内部 `ToolAgent` conversation；拒绝不会执行待审工具，而是作为反馈
  返回给模型。

### 破坏性变化

- 无。

### 迁移步骤

- 将 `dagent-ai` 从 `0.9.1` 升级到 `0.9.2`；不需要迁移数据或配置。
- 需要在项目环境中运行 `pip` 的贡献者应执行 `uv sync --extra dev` 来刷新环境。

### 验证与已知限制

- `uv run --extra dev --extra mcp --frozen pytest`
- `npm --prefix web test`
- `npm --prefix web run build`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`
- 静态 Agent 审核有意暂不支持 `MapNode`、`Subgraph` 或 `LoopNode` continuation，也不会改变
  dynamic DAG 语义。

## 0.9.1

### 变更

- Tool-loop system prompt 现在会为最终解析出的 skill scope 注入确定性的路由元数据。
  用户显式指定或与 description 明确匹配的技能会通过 `skill.view` 加载；完整
  `SKILL.md` 正文仍按需读取。
- 完整名称和 description 条目使用 8,000 字符预算；之后的仅名称条目使用独立的
  2,000 字符预算。必要时会报告省略数量，并以 `skill.list` 作为完整目录回退。
- Skill 路由条目使用紧凑 JSON array：完整条目为 `[qualified_name, description]`，
  仅名称回退为 `[qualified_name]`。

### 行为与兼容性

- `skills=None`、`skills=[]` 和显式 skill filter 保持已发布的可见性语义；request、
  checkpoint、配置和 capability id shape 均未改变。
- Qualified name 使用稳定排序；会话中选择范围和 skill metadata 不变时，system prompt
  也保持不变。
- 索引应用于 tool loop 和绑定技能的子 agent；dynamic DAG planner prompt 保持不变。

### 破坏性变化

- 无。

### 迁移步骤

- 将 `dagent-ai` 从 `0.9.0` 升级到 `0.9.1`；不需要迁移数据或配置。
- 对 tool-loop system message 做精确快照的 host，需要在最终解析出的 skill scope 非空时，
  更新快照以包含确定性的 `Available Skills` 区块。

### 验证与已知限制

- `uv run --extra dev --extra mcp --frozen pytest`
- `uv run --extra dev --frozen ruff check dagent api tests`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`
- Dynamic DAG planner prompt 有意不接收业务 skill 索引；绑定 skill 的 tool 子 agent 会在
  执行时收到自己最终解析出的索引。

## 0.9.0

### 新增

- 静态 DAG 和动态 planner 生成的 DAG 现在可以使用一等 `ConditionNode` 和 `Case`
  builder contract，实现有序、互斥的 IF/ELIF/ELSE 路由。
- `DAGEdge.branch` 把条件节点声明的 branch 连接到下游节点。一个选中 branch 可以 fan out
  到多个目标；选中但未连线的 branch 会正常结束该路径。
- `all_of`、`any_of` 和 `not_` 提供结构化、短路求值的布尔 value expressions，不引入
  script evaluation。
- Condition 执行会把 `{"branch": ...}` 记为节点 value，并把 `selected_branch` 写入
  trace node。
- Typed planner、builder-source planner、本地 FastAPI saved-DAG contract，以及 WebUI
  静态/动态编辑器都支持 condition node 和 branch edge。

### 变更

- `when` 继续作为普通边的简单独立 gate，不再用它模拟互斥分支选择。
- Condition 节点的出边必须声明该节点已有的 branch。其他 source 不能使用 `branch`，同一
  edge 也不能同时声明 `branch` 和 `when`。
- 本地 API 继续接受已发布的、没有 `type` 的 capability node request shape；新序列化的
  saved DAG 会写入规范的 `type: "capability"` discriminator。

### 行为与兼容性

- 现有 `Node`、`MapNode`、`LoopNode` 和 `add_edge(..., when=...)` workflow 保持原行为。
- Condition cases 按声明顺序检查，只选择一个 branch id；没有 case 匹配时使用必填的
  `default_branch`。
- Case 内的 node-output reference 仍要求显式上游 edge；branch 声明不会推断依赖。

### 破坏性变化

- 没有删除现有 Python builder call。使用穷举 JSON union decoder 的 host 在消费 0.9.0
  产生的 DAG 前，必须支持 `payload.type == "condition"`、可选 `DAGEdge.branch` 和可选的
  trace-node `selected_branch`。

### 迁移步骤

- 将 `dagent-ai` 从 `0.8.x` 升级到 `0.9.0`。
- 依赖上限低于 0.9 的 Enterprise host 需要更新范围，例如
  `dagent-ai>=0.9.0,<0.10.0`。
- Workflow UI 应从 condition node 声明的 handle 创建 branch edge；`when` 只保留为现有
  普通边 gate。

### 验证与已知限制

- `uv run --extra dev pytest`
- `npm --prefix web test`
- `npm --prefix web run build`
- `git diff --check`
- Condition node 有意只支持结构化表达式，不支持任意 script。
- 本版本不新增独立的 LLM、HTTP request 或 human-input node contract。

## 0.8.7

### 新增

- 本地 FastAPI backend 的 `UserDAG` 请求 contract 现在接受 JSON `output` value 或
  expression，并通过公开 `Dag.output` builder contract 传入 SDK。直接或已保存的静态
  DAG run 因此可以通过 `RunResult.output_value` 返回解析后的结构。
- Saved DAG 的创建、读取和更新流程会在现有 `spec_json` 文档中保留声明的 output。

### 变更

- `Runner(...)` 和 `Runner.from_config(...)` 现在默认使用
  `workspace=Path.home() / ".dagent"` 和 `runtime_directory=".runtime"`。
- 显式传入的值保持原有行为。`runtime_directory` 仍必须是 runner 或 run workspace 内的
  安全相对路径。

### 行为与兼容性

- 没有 `output` 的现有 saved DAG 文档会以 `output=None` 加载；不需要 API 数据库迁移或
  stored-spec 转换。
- 已显式传入运行路径的 host 会保留完全相同的存储布局。本地 FastAPI backend 继续显式
  传入自己的路径。
- `output_text` 和非静态 result 行为保持不变。持久化的 `run.finished` event 包含
  `output_value`；run summary 仍只直接暴露 `output_text`。

### 破坏性变化

- 无。

### 迁移步骤

- 将 `dagent-ai` 从 `0.8.6` 升级到 `0.8.7`。不需要迁移数据或配置。
- 希望由 SDK 管理本地存储的应用可以省略其中任意一个或两个运行路径参数。

### 验证与已知限制

- `uv run --extra dev --extra mcp --frozen pytest`
- `uv run --extra dev --frozen ruff check dagent api tests`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`
- 本地 WebUI 尚未提供声明静态 DAG output expression 的编辑器，run summary 也未直接投影
  `output_value`。

## 0.8.6

### 修复

- 使用 Pydantic 生成的 alias schema 验证 graph input 时，现在会按字段 alias dump
  Pydantic model。合法的 aliased model instance 不再在静态 run 前被错误拒绝。
- Subgraph 的 resolved input 以及 loop body 的每次迭代 input，现在都会在执行 child
  capability 前依据内嵌 `DAGSpec.input_schema` 验证。
- `RunStreamEvent.model_validate(...)` 现在根据 event envelope type 选择精确的 data
  model。即使 `response.started`/`response.finished` 或 capability completed/failed
  具有相同 payload shape，也会 round-trip 为正确的公开类型。

### 行为与兼容性

- 内嵌 input 验证失败继续使用现有 `DAGInputValidationError` contract；所属静态 DAG
  node 会失败，child capability 不会执行。
- Input validation 仍不会修改 value 或应用 JSON Schema default。`RunResult`、event
  envelope、checkpoint 和 review 行为保持不变。

### 破坏性变化

- 无。

### 迁移步骤

- 将 `dagent-ai` 从 `0.8.5` 升级到 `0.8.6`。不需要迁移数据或配置。

### 验证与已知限制

- `uv run --extra dev --extra mcp --frozen pytest`
- `uv run --extra dev --frozen ruff check dagent tests`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`

## 0.8.5

### 新增

- `validate_dag_spec(...)` 现在会检查每个 `DAGSpec.input_schema` 是否为有效且
  self-contained 的 JSON Schema Draft 2020-12 文档。
- `validate_dag_input(spec_or_schema, graph_input)` 会在静态执行前验证实例。实例不合法时
  抛出公开的 `DAGInputValidationError`，其中包含实例路径和 schema 路径。
- `RunResult.output_value` 暴露静态 `DAGSpec.output` 的精确解析值，包括 scalar、list 和
  object。序列化 result 与 `run.finished` event 都包含该字段。

### 行为与兼容性

- 无效静态 graph input 会在创建 workspace、执行 capability 或发送 `run.started` 之前被
  拒绝。验证不会修改输入，也不会应用 JSON Schema default。
- SDK schema 不限制顶层必须是 object，也不限制只能使用某一种 local reference 写法。
  只要引用资源内嵌在同一文档中，任意 Draft 2020-12 schema 都可以使用。
- `output_text` 的格式保持不变。非静态 run 的 `output_value` 为 `None`；静态 checkpoint
  仍通过 run trace 携带解析值。

### 破坏性变化

- 无。

### 迁移步骤

- 将 `dagent-ai` 依赖升级到 `0.8.5`。
- 需要结构化静态输出的 consumer 可以读取 `result.output_value`；现有 consumer 可以继续
  原样读取 `result.output_text`。

### 验证与已知限制

- `uv run --extra dev pytest`
- `git diff --check`
- JSON Schema reference 必须能从 schema 内嵌资源中解析；SDK 在验证期间不会获取外部
  schema。

## 0.8.4

### 修复

- 内置 `conversation` profile 不再介绍 `dag_agent`，也不再包含 DAG orchestration
  指令。它现在只保留 bounded tool loop 所需的通用直接回答与 tool selection 规则。

### 行为与兼容性

- `AutoAgent` 仍默认使用 `profile="conversation"` 与
  `planner_profile="dag_agent"`。独立 router prompt 继续选择 tool 或 DAG 路径；
  `ToolAgent` 和 `DagAgent` 的执行语义不变。
- 这是仅修改提示词的 SDK 修复，没有 API、schema、capability id 或配置 shape 变化。
- 现有 V4 checkpoint 仍可恢复，并继续使用其中冻结的原始 profile。升级后需要启动新 run
  才会使用修订后的内置 profile。

### 破坏性变化

- 无。

### 迁移步骤

- 将 `dagent-ai` 依赖升级到 `0.8.4`。不需要数据转换或 host 侧配置变更。
- 下游 host 应消费已发布的 SDK profile，不应复制私有替代版本。

### 验证与已知限制

- `uv run --extra dev --extra mcp --frozen pytest`
- `uv run --extra dev --frozen ruff check dagent tests`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`
- 此变更仅适用于内置 `conversation` profile。自定义 profile 以及已冻结 profile 的
  checkpoint 保持不变。

## 0.8.3

### 新增

- `Runner(extra_system_prompt=...)` 可以在 Agent Profile 和 Runtime Context 之后、
  动态 tool、capability catalog 与 DAG schema 内容之前，统一追加一段字面指令。
- 该提示词适用于 `ToolAgent`、AutoAgent 实际选择的 Tool 或 DAG 路径、DAG 初始规划
  与 replan，以及 registered agent。
- `ResolvedRunPlan` 和 `RunCheckpoint` 会冻结当前 run 的初始提示词，因此 review
  续跑不受 Runner 后续配置变化影响。

### 行为、兼容性与迁移

- `None` 会保持原有模型提示不变。对于 resolved plan 尚无此可选字段的现有 V4
  checkpoint，fingerprint 保持兼容，仍可直接恢复。
- 配置值必须是去除空白后仍非空、且不超过 16,384 个字符的字符串。SDK 按字面注入，
  不执行 Jinja、targets 或模板展开。
- 该提示词只影响模型指令，不会改变 capability 可见性、boundary、review 要求或
  workspace 权限。
- `ValidatorAgent`、`FeedbackLearnerAgent` 和 AutoAgent 的路由分类器不会收到该提示词。
- 此 patch release 没有 breaking API 或 schema 变化，也不需要迁移动作。

### 验证与已知限制

- `uv run --extra dev --extra mcp --frozen pytest`
- `uv run --extra dev ruff check dagent tests/test_agent_sdk_public_api.py tests/test_prompt_builder.py tests/test_run_checkpoint_budget.py`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`
- 静态 DAG 执行没有 planner prompt；但静态 DAG 中的 registered-agent 节点仍会收到
  Runner 提示词。

## 0.8.2

### 修复

- Dynamic DAG review continuation 在执行失败并重规划到下一个审核边界时，现在会推进
  `ConversationState.revision`，即使可见 conversation items 未改变。
  `RunResult`、`RunState` 和 `RunCheckpoint` 会暴露同一个替换 conversation 快照。
- Planner model-thread revision 现在从前一个状态版本递增，不再根据 item 数量生成。

### 兼容性与迁移

- 此 patch release 没有 breaking API 或 schema 变化。
- Host 应继续执行严格的 revision compare-and-swap 及 duplicate/stale rejection，不要为
  0.8.1 增加 equality fallback。
- 现有 V4 review checkpoint 和 V3 conversation 可在 0.8.2 上直接恢复，无需转换。

### 验证

- `uv run --extra dev --extra mcp --frozen pytest`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`

## 0.8.1

### 破坏性变更

- `Runner(...)` 和 `Runner.from_config(...)` 现在必须显式接收 `workspace` 和
  `runtime_directory` 关键字参数。SDK 不再隐式选择或创建 cwd-relative `.dagent`
  workspace。
- `runtime_directory` 是 runner 或 run workspace 内的安全相对路径。绝对路径、带 drive
  的路径、空路径、`.` 和 `..` 段都会被拒绝。
- 删除 `ResultStoragePolicy.internal_directory`。该 policy 现在只控制
  `max_inline_bytes`；所有 SDK 私有路径由 runner 统一拥有。
- `ResolvedRunPlan` 和 `RunCheckpoint` 升级到 schema version 4，并冻结
  `runtime_directory`。V3 checkpoint 会直接拒绝，不提供转换。
  `ConversationState`、`ContentReference` 和 `RunState` 仍为 schema version 3。

### 存储布局与模型上下文

- conversation backing resource 位于
  `<workspace>/<runtime_directory>/conversations`；
- 外置 tool/MCP 结果位于 `<run-workspace>/<runtime_directory>/results`；恢复到新 run
  的资源位于 `<run-workspace>/<runtime_directory>/history`；
- 这些私有目录按需创建。构造 runner、纯文本轮次和小型内联结果不会创建它们；
- continuation 会先校验并复用当前 workspace 相对路径下仍可访问的资源，只有路径缺失时
  才从 conversation backing store 恢复；
- 模型输入会包含经过预算限制和去重的外置 content、value 与 MCP artifact 引用。引用
  包含完整相对路径、媒体类型、字节数和摘要，并计入 tool-result token budget；不会暴露
  绝对路径或 conversation backing-store 路径。

### 迁移

由 host 选择两个位置，并在每个 runner 上显式传入：

```python
runner = dagent.Runner(
    workspace="agent-workspace",
    runtime_directory=".runtime",
    provider=provider,
)

runner = dagent.Runner.from_config(
    "config.yaml",
    workspace="agent-workspace",
    runtime_directory=".runtime",
)
```

把旧的结果目录选择从 `ResultStoragePolicy` 移到 runner：

```python
# 0.8.0
policy = dagent.ResultStoragePolicy(
    max_inline_bytes=64 * 1024,
    internal_directory=".dagent/results",
)

# 0.8.1
runner = dagent.Runner(
    workspace="agent-workspace",
    runtime_directory=".dagent",
    provider=provider,
    result_storage_policy=dagent.ResultStoragePolicy(
        max_inline_bytes=64 * 1024,
    ),
)
```

若要精确复现旧 cwd 布局，使用
`workspace=".dagent", runtime_directory=".dagent"`。目录迁移由 host 负责，SDK 不会
自动移动文件。升级前应完成、拒绝或取消仍在等待的 V3 review。只要引用文件或 runner 的
conversation backing resource 仍可访问，V3 conversation 可以继续使用。

如果私有 runtime 数据只需驻留内存，请把所选 workspace 或 runtime 子树挂载到 tmpfs。
SDK 不会另行维护第二套内存存储实现。

### 验证

- `uv run --extra dev pytest`
- `uv build`
- `git diff --check`

## 0.8.0

### 修复

- 内置 API 和 WebUI 已使用 0.8 `input`/`ConversationState` contract，完整持久化
  review checkpoint，并通过原子 claim 防止重复 resume。
- conversation 保留的附件以及外置 tool/MCP 结果，在下一轮使用新的 run workspace
  时仍然可访问。
- compactor 输出严格遵守 token budget；审核后的工具失败保持 failed 状态；只有带类型
  provenance 的外置结果才会被解释为 `ContentReference`。
- SQLite 升级会显式标记 pre-V3 conversation，并以 HTTP 409 拒绝，而不是尝试解析旧
  run state。
- OpenAI-compatible 流式 usage metadata 改为显式启用，使拒绝 `stream_options` 的
  endpoint 继续可用。
- API/Web model record 会保留每个模型的 context window 和 output reserve；review
  checkpoint 在多次审核门之间继续使用冻结限制。
- 静态 DAG map 输出在父 trace 中保持外置，DAG trace 会保留 value 和诊断字段标准化
  后的类型化引用。
- capability handler 异常会记录为 failed 工具结果和 failed trace node。

### 破坏性变更

- Agent run 现在接收 `input="..."` 和可选的有类型
  `conversation=ConversationState`；原始 `messages=` 参数已删除。
- 普通跨 run continuation 不再接收 `state=` 或 `checkpoint=`。下一轮应传入
  `result.conversation`。
- 审核 continuation 必须使用
  `resume(decision, checkpoint=result.checkpoint)`；state-only resume 和内存
  fallback 已删除。
- `RunState`、`RunCheckpoint` 和 `ResolvedRunPlan` 使用 schema version 3。
  V1/V2 payload 会被拒绝，runtime 不提供转换层。
- 删除 `RunResult.messages`、`RunState.internal_messages` 和
  `RunState.input_message_count`。
- 删除 `Provider.strip_thinking`。推理捕获由 `reasoning.capture` 控制，并与可见回答
  分开保存。
- OpenAI-compatible provider 返回非法 JSON 或非对象 tool-call arguments 时会明确
  失败，不再静默转换为空参数对象。

### 新增

- 与 provider 无关的 `ConversationState`、`UserMessage`、`AssistantMessage`、
  `ToolResultMessage`、`ToolCallItem`、`Attachment`、`ContextSummary` 和
  `ContentReference`。
- tool agent、DAG planner、router、validator 和注册子 agent 共用统一上下文组装器；
  每次调用前统一计算 system prompt、schema、历史、摘要和工具结果预算。
- 模型摘要及明确的确定性 fallback、单个/总工具结果限制、调用前
  `ContextWindowExceeded`、类型化 context usage 和压缩流事件。
- assistant 审计 item 上的 reasoning 和 provider token usage；reasoning 永不回放。
- 大型文本和二进制工具/MCP 结果会原子写入 run workspace，并使用 SHA-256 引用。
- review checkpoint 会冻结 capability definition 指纹，并在执行获批 tool/MCP call
  前完成校验。

### 迁移

```python
# 0.7
messages = [{"role": "user", "content": "记住蓝色。"}]
first = await runner.run(agent, messages=messages)
messages += first.messages
messages.append({"role": "user", "content": "什么颜色？"})
second = await runner.run(agent, messages=messages, state=first.state)

# 0.8
first = await runner.run(agent, input="记住蓝色。")
second = await runner.run(
    agent,
    input="什么颜色？",
    conversation=first.conversation,
)
```

Host 必须为待审核 run 持久化完整 V3 checkpoint，并为聊天 continuation 持久化完整的
有界 `ConversationState`。参见 [0.8 Host 迁移](host-migration-0.8.md)。

## 0.7.6

### 新增

- Profile-backed 模型调用的 system prompt 现在会收到动态 `Runtime Context` 段，
  其中包含解析后的 workspace root，并要求相对文件路径从该目录解析。`Runner` 会把
  run workspace 传给 tool agent、动态 DAG planner、注册到 DAG 的子 agent 和结果
  validator，同时不修改 Profile Markdown。
- 底层 `ProfiledAgent`、`ValidatorAgent` 和 `FeedbackLearnerAgent` 调用新增可选
  `workspace_path`，用于生成相同的 prompt context。

### 改变

- 注册到 DAG 的子 agent 改为使用共享 PromptBuilder 的 workspace context，不再在
  DAG context 中维护单独的 workspace 行。Profile 内容保持不可变，也不会被当作模板。

### 迁移

- `Runner` 管理的 agent 无需迁移。显式传入的
  `Runner.run(..., workspace_path=...)` 会按原值注入；省略时，runner 会注入解析后的
  managed run workspace。
- 直接调用 `FeedbackLearnerAgent` 时，如果该底层 helper 需要 workspace context，
  可以传入 `workspace_path`。
- 此 patch release 没有 breaking change。

### 已知限制

- `FeedbackLearnerAgent` 不归 `Runner` 管理，因此未传 `workspace_path` 的直接调用
  不会收到 workspace 段。

### 验证

- `uv run --extra dev --extra mcp --frozen pytest`
- `uv build`
- `uv run --with twine python -m twine check <distributions>`
- `git diff --check`

## 0.7.5

### 新增

- 静态和动态 DAG 的 agent capability 调用新增可选参数 `reference_content`。非空内容
  会作为 task data 放入独立的 user-message 区块；内容为空时不改变组装后的 prompt。
  schema 驱动的 WebUI 会展示该参数，并支持绑定 graph input、artifacts 或上游节点输出。
- Web 静态 DAG 的字符串参数现在会识别 `{{ variable }}` 模板。模板编辑器可以在光标位置
  插入并直接绑定选中的变量；手动输入的占位符必须通过选择器显式绑定。编辑器仍持久化现有
  的结构化 `format` expression，因此 SDK 和 API request shape 不变。无法通过可视化语法
  无损往返的已有 format expression 会保持原样并以只读方式展示。
- Web DAG 画布现在会区分条件边并显示简短的条件标签。静态 DAG 边检查器可通过受作用域
  限制的变量选择器编辑真值判断和比较条件；不支持的表达式保持只读并原样保留。

### 改变

- Internal `typed_spec` planner response 现在只包含可执行意图。Graph name/description、
  node title 和 edge reason 不再由模型生成；host 负责补齐 canonical graph identity，纯展示
  字段的变化也不会再让已完成 DAG 结果失效。
- 默认 `typed_spec` planner contract 现在只接受 capability node；模型侧的 Map、Subgraph、
  Loop 和 item expression 构图继续由 `sdk_builder` 提供，public static-DAG SDK 也保持完整
  支持。Canonical `DAGSpec` 执行能力不变。
- Structured planner call 现在统一使用一条传输路径：完整 compact schema 注入 system
  prompt，内置 OpenAI-compatible provider 请求 `response_format.type="json_object"`。
  SDK、YAML、API 和 WebUI 中的 `structured_output_mode` 设置已删除。

### 迁移

- 现有 agent 节点无需修改，因为 `reference_content` 默认是空字符串。
- 返回 `typed_spec` planner fixture 的 deterministic provider 需要删除上述展示字段。
  Map、Subgraph 或 Loop proposal 需要改写为 capability-node graph；如果模型必须生成复杂
  控制流，请改用 `sdk_builder`。Canonical `DAGSpec` 和公开 static-DAG SDK shape 保持不变。
- 从 Provider 构造、配置文件和模型管理 payload 中删除 `structured_output_mode`。

### 验证

- `uv run --extra dev --extra mcp --frozen pytest`
- `npm --prefix web test`
- `npm --prefix web run build`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`

## 0.7.4

### 新增

- `Runner(..., mcp_stdio_stderr="inherit")` 允许负责进程监管的 host 显式把
  stdio MCP server stderr 转发到 host 进程。`Runner.from_config(...)` 接受同一个
  显式 host-policy 参数；派生 runner 默认继承父 runner 的设置，除非调用方覆盖。

### 改变

- stdio MCP server stderr 现在默认丢弃。SDK 不再创建或追加
  `~/.dagent/logs/mcp-stderr.log`，因为 server stderr 可能包含凭据或其他敏感值。

### 迁移

- 以前读取 SDK MCP stderr 文件的 host，必须改为显式设置
  `mcp_stdio_stderr="inherit"`，并在自己的进程监管边界进行有界、脱敏的 stderr
  捕获。不需要 MCP stderr 的 host 无需改动。

### 验证

- `uv run --extra dev --extra mcp --frozen pytest`
- `git diff --check`

## 0.7.3

### 新增

- Dynamic DAG planning 现在使用 internal strict JSON Schema contract，并显式区分
  `propose_plan`、`no_change` 和 `final_answer`。
- 类型化 dynamic plan 支持 capability/agent、map、subgraph、bounded loop、条件边、
  artifacts、graph output 和递归 value expressions；review/执行前统一规范化为 canonical
  `DAGSpec`。
- `RunState.dag_spec` 和 `PendingReview.proposed_dag_spec` 会在 SDK checkpoint 和 review
  continuation 中保留 canonical dynamic spec。
- `dagent.providers.StructuredOutputFormat` 是 provider-neutral structured response
  contract；`ChatResponse.refusal` 用于携带 provider refusal。
- `Runner(..., planner_frontend="sdk_builder")` 及对应 YAML 顶层设置新增 optional
  restricted SDK Builder planner。它支持初始规划和 full-spec replan，且不会执行模型生成的
  Python。
- 新 V2 checkpoint 会记录 `planner_frontend`；Builder plan 会冻结 versioned
  `generate-dag` skill 内容和 digest，以便确定性 resume。

### 改变

- Dynamic planner 直接引用稳定 capability id；kind、defaults、risk、boundaries 和
  invocation identity 由 host 补齐。
- Full-spec replan 会保留未变 invocation id，修改已完成 node 时要求 `rerun_nodes`，并按
  实际变化让下游结果失效。待审核 DAG 会持久化 `rerun_nodes`，artifact 定义变化也会参与
  review 和结果失效判断。
- `OpenAICompatibleProvider` 会在 stream 和 non-stream 调用中把 planner schema 映射为
  Chat Completions `response_format.type="json_schema"`。
- 本地 API 把 `planner_frontend` 作为 service-wide Runner setting；request payload 和
  WebUI 不提供 per-request frontend selector。
- WebUI 对长会话只渲染最近一段消息，并支持逐步加载更早消息；会话历史区域也会复用稳定
  结果，减少无关状态更新引起的重复渲染。

### 修复

- `Runner.cancel(run_id)` 现在会把取消信号传递到 active runtime work、async
  capability、MCP call 和内置 shell process group。WebUI 停止按钮会调用后端取消接口，
  不再只是关闭本地 event stream。
- 已拒绝或已经结束的 review request 在重新打开历史会话时不会再次恢复为 pending review
  对话框。
- Typed planner schema 现在会保留 required planner fields，并使用 provider 支持的 strict
  structured-output union。Replan 会正确处理 artifact revision、review checkpoint 中的
  explicit rerun、变更后的 artifact state，以及没有 schema 的 nested input。
- Builder replan 会保留与 host field 同名的用户 argument key，支持校验 composite
  subgraph output path，并始终把完整 canonical DAGSpec 作为权威 replan context。

### 破坏性改变

- Free-form PlanSpec DSL 及其 parser/compiler types 已移除。Dynamic planner 只接受 JSON，
  没有 legacy fallback。
- Custom `ChatProvider` 的 `chat(...)` 和 `stream_chat(...)` 必须接受 keyword-only
  `response_format`，并在 dynamic DAG planning 中遵守该 contract。
- 旧 PlanSpec path 创建的 review checkpoint 不含 canonical proposed spec，不能作为 typed
  DAG review 恢复。

### 迁移步骤

- 把 deterministic provider 和 test fixture 从 PlanSpec 文本或裸 `NO_CHANGE`/final text
  改为 schema-valid planner JSON。
- Custom provider 应传递 `StructuredOutputFormat`、返回符合 schema 的 JSON，并用
  `ChatResponse.refusal` 暴露 structured-output refusal。
- 在持久化新 checkpoint 前，用当前版本重新创建 pending dynamic DAG review；不要隐式转换
  旧 PlanSpec review state。
- 现有 V1 checkpoint 无需迁移；它们继续可读并使用 `typed_spec`。持久化新 V2 checkpoint
  时，不要删除 frontend 或冻结的 planner-skill fields。

### 验证

- `uv run --extra dev --frozen pytest`
- `npm --prefix web test`
- `npm --prefix web run build`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`

### 已知限制

- 第一阶段使用完整 typed graph replan；atomic typed patch 延后。
- `typed_spec` 仍是默认值。`sdk_builder` 只接受文档定义的 straight-line AST subset，
  不是通用 Python authoring environment。
- Atomic typed/Builder patch operation 仍延后；两个 frontend 都使用完整 graph replan。

## 0.7.2

### 改变

- 内置 `conversation` profile 现在显示为“通用智能体”，并更明确地在简单任务中优先使用
  能直接完成请求的可用工具，同时只为复杂编排使用 `dag_agent`。

### 修复

- `tool.shell` 现在会在专用进程组中启动命令，并在超时时终止整个进程组，包括管道中的
  子进程。即使逃逸进程仍持有继承的输出管道，超时清理也有明确时限；已经捕获的输出会保留
  在终态超时错误中。

### 破坏性改变

- 无。

### 迁移步骤

- 此补丁版本不需要迁移动作。必须调用特定工具的工作流仍应使用专用自定义 profile；内置
  profile 的工具选择仍由模型自主决定。

### 验证

- `uv run --extra dev --frozen pytest`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`

### 已知限制

- 通用智能体对直接工具的偏好属于 prompt 指引，不保证强制调用。

## 0.7.1

### 新增

- `ResolvedRunPlan` 记录 SDK 已解析的目标专属 profiles、局部 loop limits、精确的
  capability/agent/skill scope、validation settings 和 run-wide limits。它不包含
  handlers、provider secrets、connections 或 host policy。
- `RunCheckpoint` 组合 `RunState`、`ResolvedRunPlan` 和累计 `ExecutionUsage`。
  SDK 生成的 result 通过 `result.checkpoint` 暴露它；
  `Runner.run_checkpoint(run_id)` 返回最新的内存或 terminal checkpoint。
- `ExecutionLimits` 提供互相独立的 total-operation、model-turn 和
  capability-call 上限。Root agents、DAG work、validation、retries、并发 branches 和
  subagents 共享同一个预留对象。
- `ExecutionLimitExceeded` 会在某次操作越过限制之前失败。

### 改变

- 跨进程 review continuation 现在使用 `Runner.resume(..., checkpoint=...)` 或
  `Runner.resume_stream(..., checkpoint=...)`。Resume 会校验精确 scope，重建目标专属
  derived runtime，并在应用 review decision 前恢复 usage。
- `Runner.run(..., checkpoint=...)` 会为普通跨进程 continuation 恢复 usage 和原始
  limits。同一个 Runner 上的 `state=...` continuation 使用匹配的 checkpoint cache，
  并拒绝过期 state。
- Resolved profile snapshots 现在深层冻结。每个 plan 都携带 canonical SHA-256
  fingerprint；checkpoint hydration 会拒绝不一致的 pending capability review，以及
  resolved scope 外的 invocation。
- Checkpoint review decision 在一个 `Runner` 内只能消费一次。Resume 失败会记录 terminal
  checkpoint；budget exception 通过 `error.checkpoint` 和 `error.usage` 暴露它和更新后的
  usage。
- SDK 生成的 `RunState.capability_scope` 现在记录已解析的 canonical skill IDs 和精确
  capability IDs，而不是未解析声明。
- `RunResult.model_dump(...)` 保持原有 `state` 和 `output_text` payload shape。
  Portable review continuation 应单独持久化 `result.checkpoint`。

### 破坏性改变

- 当不存在匹配的内存 checkpoint 时，`Runner.resume(..., state=...)` 和
  `resume_stream(..., state=...)` 已弃用并会产生 `DeprecationWarning`。它们在 v0.7.1 中
  仍作为显式 legacy path 保留，但无法恢复目标专属执行语义。
- 当所需 capability 或 skill 不可用时，checkpoint resume 不会回退到 base runtime，
  而是 fail closed。
- 恢复的 limits 不能替换或扩大。Durable multi-process host 必须在执行前原子认领
  review ID，避免跨进程 stale-checkpoint replay。

### 迁移步骤

- 对等待 review 的 Run 持久化 `result.checkpoint.model_dump_json()`。
- 使用 `RunCheckpoint.model_validate_json(...)` 恢复，构造兼容的 `Runner`，并把
  checkpoint 传给 `Runner.resume(...)`。
- 后端重启后的普通 bounded continuation 应把恢复的 checkpoint 传给
  `Runner.run(...)`，而不只是传 state。
- 如有需要，可把 host 级 step policy 映射到
  `ExecutionLimits.max_total_operations`。不要改写 `ToolAgent.max_steps` 或
  `DagAgent.max_cycles`；它们仍是局部 loop controls。
- Checkpoint storage、tenant authorization、retention，以及兼容 capability/provider 的
  构造继续由 host 负责。

### 验证

- `uv run --extra dev pytest`
- `uv build`
- `git diff --check`

### 已知限制

- Checkpoint 描述执行语义，不包含可执行 handlers 或 provider connections。恢复它的
  host 必须构造兼容的 `Runner`。
- Plan fingerprint 用于发现意外修改，不是签名；checkpoint authenticity 仍由 host 负责。
- 静态 DAG result 现在可通过 `Runner.resume(..., checkpoint=...)` 恢复受支持的直接
  Agent 节点工具审核。`MapNode`、`Subgraph` 和 `LoopNode` 中的 Agent 组合会被明确拒绝；
  通用静态 DAG crash continuation 仍不受支持。

## 0.7.0

### 新增

- `Runner.derive(...)` 可以通过 `inherit_local_tools=True` 继承本地
  `CapabilityBinding` registrations，并通过 `exclude_local_tool_ids` 排除调用方管理的
  tool IDs。
- `Runner.run(...)` 和 `Runner.stream(...)` 接受调用方提供的 Run ID；同一个
  `Runner` 上重复的新 Run ID 会被拒绝。
- 带版本的 `RunState` schema v1 支持 JSON round trip，以及通过
  `Runner.resume_stream(...)` 在同一个 `Runner` 或调用方恢复状态后显式继续。
- Capability reference 校验不会修改调用方拥有的 agents、DAGs、profiles 或
  bindings；validation issues 在 hydration 后仍保留类型字段。
- SDK 生成的 MCP snapshots 支持 fail-closed lazy connection，同时保留
  canonical capability identity、当前 filters 和当前 policy。

### 改变

- 达智重新保持为进程内 SDK library。进程生命周期、命令协议、健康检查、持久化、
  凭证、调度和容器生命周期由调用方 host 负责。
- 调用方提供的 Run ID、带版本的 `RunState`、capability reference 校验、MCP
  snapshots、lazy MCP 连接和 `Runner` 清理继续作为公共 library 行为保留。

### 修复

- Lazy MCP registration 现在会在启用的 server 缺少 snapshot 时失败，拒绝非
  canonical snapshot identity，并重新应用当前 filters 和 policy。
- 并发的首次 MCP 调用现在只会进行一次串行化 server startup。
- 同一个 `Runner` 上不同的新 Run 不能重复使用调用方提供的 Run ID。
- `CapabilityBinding` 冲突和 hydration 后的 `ValidationIssue` 会保留机器可读字段。
- Stream cancellation 会等待 SDK-owned execution task 完成清理。

### 破坏性改变

- 删除 `RuntimeRunSpec`、`RuntimeFrame`、runtime transport schemas 和
  `python -m dagent.worker`。历史 v0.6.9 tag 保持不变。
- 这是一次有意的 pre-1.0 删除。项目目前不知道有 v0.6.9 process API 使用者，
  但使用该 API 的调用方必须迁移。

### 迁移步骤

- 在 host 进程内构造 `Runner`，并用 `Runner.stream(...)` 启动新执行。
- 使用 `Runner.resume_stream(...)` 继续待审核 Run；调用方管理恢复时传入显式
  `RunState`。
- 进程命令、凭证、持久化、重试、健康检查和容器生命周期留在 host。
- 需要 fail-closed lazy MCP registration 的 host 应持久化 SDK 生成的 MCP
  snapshots，不要在 SDK 外部重建 MCP capability IDs。

### 验证

- `uv run --extra dev pytest`
- `uv build`
- `git diff --check`

### 已知限制

- 达智不提供 process host、service loop、durable store 或活跃 Run 的透明恢复。

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
