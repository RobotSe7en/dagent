# 核心概念

dagent 将声明式配置和运行时所有权分离。这样 agent 对象保持轻量，而执行状态、
capabilities、review 和 continuation 都是显式的。

## Runner

`Runner` 是执行入口的公开 SDK。它拥有：

- provider
- capability catalog
- MCP server 注册
- skill discovery 和 managed installs
- profile roots
- runtime state 和 traces
- review continuations

当你的应用在代码中提供配置时，直接构造 `Runner(...)`。当 provider settings、MCP
servers、validation 或 profile directories 应从 YAML 文件加载时，使用
`Runner.from_config(...)`。

## 声明式 Agents

`ToolAgent`、`AutoAgent` 和 `DagAgent` 是不可变的 run targets。它们声明一次运行
应使用的 profile、capabilities、skills、bounds 和 review level。它们不拥有
provider clients、sessions 或 capability handlers。

## Tool Loops 和 Dynamic DAGs

`ToolAgent` 执行有边界的 tool-loop 工作。它适合每一步都依赖最新 observation 的任务。

`DagAgent` 会让模型规划一个可 review 的 capability-node DAG，执行 ready layers，
观察结果，并在需要时进行局部 replan。

`AutoAgent` 让 runtime 针对每个请求在直接 tool use 和 dynamic DAG planning 之间选择。

## 静态 DAGs

当 workflow 的图结构属于代码时，使用 `Dag`。静态 DAG 使用类型化 graph input、
显式 `dag.add_edge(...)` 依赖、结构化 `$expr` value references、artifact 声明、
boundaries、conditional edges、map fan-out、subgraphs 和 bounded loops。

## Capabilities

Capabilities 是 runner 已知的可执行对象：

- Python tools 使用 `tool.<name>` ids。
- MCP tools 使用 `mcp.<server>.<tool>` ids。
- 内置 skill accessors 使用 `skill.list` 和 `skill.view`。

Agents 接收 capability ids 或 `@dagent.tool` bindings 的 allowlist。runtime 通过共享
capability executor 执行所有 capability kinds。

## Skills

Skills 是从 skill roots 或 managed installs 中发现的可读 instruction assets。它们
本身不是可执行 capability。Agents 使用 `skills=[...]` 限制通过 `skill.list` 和
`skill.view` 可见的具体 skills。

## Results、State 和 Review

`Runner.run(...)` 会为每一种公开 target 返回 `RunResult`。Agent runs 接收
OpenAI-compatible `messages`；静态 DAG runs 接收 `graph_input`。

`RunResult.messages` 只包含当前 run 生成的 messages。调用方拥有 conversation list，
并且在继续 agent conversation 时应追加这些 messages。`RunResult.state` 包含 dagent
可恢复的内部状态，包括 trace data 和 pending review checkpoints。

当工作需要 review 时，用 `Runner.resume(...)` 或 `Runner.resume_stream(...)` 批准或拒绝。
跨进程 continuation 应持久化 `RunResult.checkpoint`；它把可变 state、已解析的目标执行语义
和累计 operation usage 分开保存。可选的 `ExecutionLimits` 会让 root、DAG、validation、
并发和 subagent work 共享同一个 budget。详见[结果、流式输出和 Review](results-streaming-review.md)。
