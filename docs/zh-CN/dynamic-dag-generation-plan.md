# 动态 DAG 生成计划

本文记录动态 DAG 生成的分阶段技术路线。两个阶段均已在 0.7.3 中提供。
已发布版本行为以各任务指南为准。公开 API、capability id、配置语义和 review contract
仍是明确 contract。

## 状态

- 第一阶段：已实现 internal strict planner schema、完整初始 plan 和完整 plan replan。
- 第二阶段：已实现 optional `sdk_builder` planner frontend；默认仍为 `typed_spec`。
- Canonical representation：`DAGSpec`，dynamic `RunState` 和 pending DAG review 会同时
  保存它与 executable DAG projection。
- 旧 free-form PlanSpec DSL：已直接移除，没有 fallback parser。

## 目标架构

静态 DAG 和动态 DAG 应共享同一个规范化执行表示：`DAGSpec`。两者的区别只在于计划的
来源，而不在于校验、review 和执行语义：

```text
Python Dag builder ───────────────┐
                                  ├─ canonical DAGSpec
Typed dynamic planner output ─────┤        │
                                  │        ├─ validation
Restricted SDK builder（阶段二）──┘        ├─ review
                                           └─ execution
```

核心原则：

- `DAGSpec` 是持久化、fingerprint、review 和执行使用的唯一 canonical IR。
- 默认 typed planner 有意采用精简的 capability-node contract；模型生成的复杂控制流隔离在
  optional restricted Builder frontend 中。
- planner 只声明执行意图；provider、handler、risk、boundary、workspace、运行状态和
  invocation identity 仍由 `Runner` 与 host 拥有。
- 所有 planner frontend 最终进入同一套 normalization、validation、review 和 execution
  路径，不保留重复执行实现。
- 条件使用结构化表达式；Builder 生成的 Map 和 Loop 使用显式上限。不接受任意可执行条件
  代码或无界控制流。

## 阶段一：类型化 Planner Spec 到 DAGSpec

第一阶段先建立类型化、声明式的动态规划协议。优先使用 provider structured output，避免
继续扩展当前自由文本 PlanSpec DSL。

### Planner 输出协议

- 使用显式 discriminator 区分以下响应，不再通过“尝试解析失败”猜测响应含义：
  - `propose_plan`
  - `no_change`
  - `final_answer`
- planner-facing spec 与 `DAGSpec` 尽量一一对应，但排除 graph name/description、node
  title 和 edge reason 等 host-owned identity/display 字段。
- typed response contract 只保留 capability/agent invocation node；固定并行任务使用多个
  capability node 表达。Map fan-out、embedded Subgraph 和 bounded Loop 继续通过
  `sdk_builder` 与 public static-DAG SDK 提供。
- edge 支持结构化 `when` condition。
- 支持 graph input、node output/content/status/steps、artifact、format 和 comparison value
  expressions。
- 支持显式 DAG output 和 artifact producer/consumer 声明。

### Capability 上下文

Planner 应看到经过 scope 过滤的真实 capability catalog，包括：

- stable capability id、planner-visible name、kind 和 description；
- 完整 input schema：类型、required、enum、default 和嵌套字段；
- 完整 output schema，使 planner 能选择结构化 output path 和构造条件；
- 已注册 agent 的参数契约和局部执行上限。

模型不得自行注册 capability、provider、MCP server 或 agent。模型也不得决定 risk、扩大
boundary，或覆盖 host policy。

### 规范化和执行路径

Planner 输出按以下顺序处理：

1. 解析并校验 planner-facing Pydantic contract。
2. 使用稳定 capability id 在 scope-filtered catalog 中解析 capability。
3. 从 catalog 补齐 kind、risk、boundary、default arguments 和其他 host-owned metadata。
4. 生成 canonical `DAGSpec`。
5. 调用 `validate_dag_spec(...)`，按所有 DAG source 共用的 canonical contract 检查依赖、
   value expressions 和 artifacts。
6. 对规范化后的 `DAGSpec` 进行 review、fingerprint 和执行。

Validation 失败时，应把结构化字段路径和具体错误返回 planner 修复。未知字段或不支持的
控制流必须显式失败，不能静默丢弃，也不能被误判为最终答案。

### 初始规划和 Replan

先把初始完整 DAG 生成做稳定，再扩展动态 replan：

1. 生成、校验、review 和执行完整 typed spec。
2. 使用另一个完整 typed spec 做 replan；未改变的 invocation identity 保持稳定，修改已完成
   node 时必须显式请求 rerun。
3. 后续 milestone 引入带 `base_version` 的 typed patch，原子地添加、替换或删除节点、边和参数。
4. 默认禁止无意修改已完成节点；确实需要重新执行时必须显式声明。
5. 结果失效应基于实际修改及其下游，不应因为任意 edge 变化就让所有节点失效。

在 replan 协议稳定前，可以关闭或限制逐层 `dynamic_adjust`，避免把首次规划质量、执行反馈
和图修复问题混在一起调试。

### 第一阶段验收标准

- Structured output 能稳定区分 plan、no-change 和 final answer。
- typed planner 能生成并执行条件边和并行 capability node 的代表性用例；Builder frontend
  覆盖 Map、Loop 和 Subgraph 构图。
- 所有 capability 引用、output path 和 artifact dependency 都经过 fail-closed validation。
- Planner 无法声明或扩大 host-owned risk、boundary 和 runtime configuration。
- Parser 不再静默忽略未知 planner 行或字段。
- 当前 PlanSpec DSL 的保留、弃用或迁移策略必须明确记录；不增加隐藏兼容路径。

## 阶段二：受限 SDK Builder 到 DAGSpec

第二阶段把公开 Python DAG builder 作为可选的模型 authoring frontend，以利用模型更成熟
的代码生成能力。该阶段不改变 canonical IR，也不增加第二套 validator 或 executor。

通过 `Runner(..., planner_frontend="sdk_builder")` 或 YAML 顶层的
`planner_frontend: sdk_builder` 全局启用。API request 和 WebUI 不提供 per-request selector。
初始规划和 replan 都返回完整 Builder 程序。Validation failure 继续消耗现有 planner cycle
budget，不增加独立 repair loop。

### DAG Generation Skill

提供一个专门的 DAG generation skill，内容包括：

- 精简且版本明确的公开 SDK reference；
- capability 和 registered agent 的使用规则；
- 条件边、结构化 output reference、Map、Loop、Subgraph 和 artifact 示例；
- bounded control-flow、boundary 和 review 规则；
- 常见 validation 错误与修复示例；
- 生成和验证入口的说明。

Planner 必须显式加载该 skill 的内容。仅把 skill 安装到 `SkillStore` 或放入 agent scope，
不视为 planner 已读取并遵守它。

### 受限 Builder Contract

模型只生成纯构图代码，例如创建 `Dag`、`Node`、`MapNode`、`LoopNode`，添加节点和边，
以及声明 output。生成代码不得作为普通 Python 任意执行。

允许的能力至少包括：

- 变量赋值；
- literal、list、dict；
- 已批准 DAG builder constructor 和方法；
- graph input、node output、item、artifact 和 comparison references；
- 受控的 subgraph 构造。

必须禁止：

- import、文件、网络、subprocess 和环境变量访问；
- `eval`、`exec`、dunder 和任意函数调用；
- 构图期间执行真实 capability；
- 任意副作用；
- 无界循环和不能静态审计的构图逻辑。

实现使用受限 AST interpreter，在不调用 `exec` 或 `eval` 的前提下，把允许的 builder
statement 转成真实公开 Builder values。Source 上限为 64 KiB、10,000 个 AST nodes、表达式
深度 64。只接受 straight-line assignments、JSON-like values、批准的 constructors 和
`Dag` methods、references 与 comparisons。Import、definition、control flow、comprehension、
任意 call、dunder access 和 host-owned arguments 都会在生成 `DAGSpec` 前 fail closed。

### Capability 和 Agent 引用

生成代码只引用 catalog 中已经注册的稳定 id，例如：

```python
target="tool.search"
target="mcp.browser.open"
target="agent.analyst"
```

模型不得定义 handler、构造 provider、注册 MCP server，或创建拥有运行时状态的 agent。
Builder 得到 `Dag` 后立即调用 `to_dag_spec()`；之后的 normalization、validation、review、
持久化和执行全部只使用 canonical `DAGSpec`。

### 第二阶段验收标准

- 任意非构图 Python 语法都被执行前拒绝。
- 相同 builder 输入规范化为稳定、可重复的 `DAGSpec`。
- SDK frontend 和 typed-spec frontend 共享完全相同的 capability resolution、validator、
  review 和 executor。
- 如果 typed-spec frontend 已达到质量目标，第二阶段保持可选，不因技术路线预设而强制替换
  第一阶段。

## 明确不做的事情

- 不直接执行不受约束的模型生成 Python。
- 不让 skill 承担 runtime、provider、capability registration 或安全边界职责。
- 不让模型输出的源码成为 review 或持久化的权威对象。
- 不为两个 frontend 维护两套 DAG 语义、validation 或 execution code path。
- 不为了兼容当前 DSL 而引入静默转换、旧 capability alias 或模糊的 fallback parser。

## 已确定决策与剩余工作

- Planner-facing typed contract 保持 internal；`DAGSpec` 是共享 public/canonical contract。
- 第一阶段使用 full-spec replan；typed patch 操作延后。
- Planner capability reference 使用稳定 capability id。
- Planner value 使用递归 typed AST；host 将其转换为 native literal 和 `$expr` binding。
- 沿用现有 runtime execution/cycle bounds；第一阶段不增加独立 graph-size policy。
- Free-form PlanSpec DSL 已直接移除。持久化的 deterministic planner fixtures 和 custom
  providers 必须迁移到 structured output。
- 内置 `generate-dag` skill 是 mandatory 且 version-locked；其完整内容和 digest 会冻结在
  V2 checkpoint 中，并在 resume 时恢复。
- Builder source 只保留在 planner transcript；canonical `DAGSpec` 才是 review、持久化、
  fingerprint 和执行的权威对象。
- Checkpoint V1 继续可读，并明确表示 `typed_spec`；新生成 checkpoint 使用 V2 并记录所选
  frontend。
- 本阶段不建立评测基线，也不设置 A/B gate。
