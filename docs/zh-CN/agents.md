# Agents

达智有三种公开 agent 配置：`ToolAgent`、`AutoAgent` 和 `DagAgent`。它们都是声明式
run targets。`Runner` 拥有 provider clients、capabilities、runtime state、review
continuation 和 execution dispatch。

## 选择 Agent

| Agent | 适用场景 |
| --- | --- |
| `ToolAgent` | 模型应使用有边界的 tool loop，并根据最新 observation 选择下一步动作。 |
| `AutoAgent` | runtime 应针对每个请求选择直接 tool use 或 dynamic DAG planning。 |
| `DagAgent` | 模型应规划可 review 的 DAG，执行 ready layers，观察结果，并进行局部 replan。 |

当图结构属于代码时，使用静态 `Dag` 而不是 agent。见[静态 DAG](static-dag.md)。

## 受管 Profiles 和 Agent Presets

内置 profiles 位于 `dagent/resources/profiles/*.md`。`dag_agent` 是执行 planner profile，
`dag_design` 是 `Runner.design_dag(agent=None)` 使用的专用非执行型 profile；它不会作为
可运行的 `agent.dag_design` capability 发布或被接受。本地 FastAPI 服务会把可编辑
profiles 管理在 `~/.dagent/profiles/<name>.md` 下；用户可以创建、复制、编辑和删除
这些 profiles，而不必在每次 run 时传 Markdown 文件路径。

受管 profile 名称也是 agent capability 的产品标识，必须以字母开头，并且只能包含字母、
数字和 `_`。名为 `analyst` 的受管 profile 会在静态 DAG 编辑器中暴露为
`agent.analyst`。

本地 API 还会把可复用 agent preset 存在 `~/.dagent/agents/*.json` 下。一个 preset
会选择 profile，并固定这个子 agent 可用的 tools、MCP capabilities 和 skills。聊天和
dynamic DAG runs 可以用 `agent_scope="selected"` 加 `agent_ids=["agent.<name>"]`
暴露指定 preset，也可以用 `agent_scope="registered"` 暴露所有已注册 presets。

Preset JSON 使用 `ToolAgent` 字段名：`name`、`profile`、`capabilities`、`skills`、
`agents`、`review`、`max_steps` 和 `description`。已注册 presets 是叶子子 agent，
因此 `agents` 必须为空，`review` 必须是 `"fast"`。本地 API 会先校验 preset，再写入
workspace；`capability_ids` 这类旧字段会被拒绝，不会自动转换。

## ToolAgent

```python
import asyncio

import dagent


@dagent.tool
def echo(text: str) -> str:
    return f"echo:{text}"


async def main():
    runner = dagent.Runner(
        workspace="agent-workspace",
        runtime_directory=".runtime",
        provider=provider,
        capabilities=[echo],
    )
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["tool.echo"],
        skills=["writing/terse"],
        max_steps=8,
        review="fast",
    )

    result = await runner.run(
        agent,
        input="Use echo to respond with hello.",
    )
    print(result.output_text)
    runner.close()


asyncio.run(main())
```

运行离线示例：

```bash
uv run python -m examples.tool_agent
```

### 工具选择

内置 `conversation` profile 的显示名称为“通用智能体”。它会在某个可用工具能直接完成用户
请求时优先调用它，包括简单的单工具任务。它仍会直接回答问候以及没有明确相关工具的请求，
也不会仅因工具已启用就调用。它是纯 tool-loop profile，不包含 DAG 选择或规划指令。这属于
模型自主选择，不保证强制调用；若工作流必须使用特定工具，请使用带有明确指令的专用自定义
profile。

## AutoAgent

`AutoAgent` 没有 mode 字段。它通过独立 router prompt 为每个请求路由到直接 tool use 或
dynamic DAG planning。tool 分支使用 `profile`（默认 `conversation`），DAG 分支使用
`planner_profile`（默认 `dag_agent`）。

```python
agent = dagent.AutoAgent(
    profile="conversation",
    planner_profile="dag_agent",
    capabilities=["tool.search"],
    skills=["research/briefing"],
    max_steps=8,
    review="fast",
    dynamic_adjust=True,
)

result = await runner.run(
    agent,
    input="Answer directly or plan if orchestration helps.",
)
```

运行离线示例：

```bash
uv run python -m examples.auto_agent
```

## DagAgent

`DagAgent` 用于 dynamic DAG planning。它可以在执行 proposed work 前暂停等待 human review。
Planner 调用使用严格 JSON Schema 响应：模型显式选择 `propose_plan`、`no_change` 或
`final_answer`。Runner-wide planner frontend 决定 proposal 是类型化 plan 还是受限 Builder
source；两者都会先规范化为 canonical `DAGSpec`，再进入校验、review 和执行。Capability
node 使用 `tool.search` 这类稳定 id；kind、risk、boundary、defaults 和 invocation identity
由 host 补齐。

类型化动态 plan 支持 capability/agent node、有序 condition node、branch edge、条件边
gate、artifacts、graph output 和 structured value references。为了保持模型侧 contract
紧凑，typed planner 不开放 Map、
Subgraph 和 Loop 构图。Graph identity 由 host 补齐，纯展示用途的 graph description、
node title 和 edge reason 不要求模型生成。规范化后的 plan 仍与静态 DAG 共用 validator
和 executor。

`typed_spec` 仍是默认值。需要 code-oriented authoring 时，可用
`planner_frontend="sdk_builder"` 构造 runner。模型源码只允许 straight-line、allowlisted
Builder subset，绝不会传给 `exec` 或 `eval`。它还可以表达 Map、Subgraph、bounded Loop、
artifact、reference、output、condition node、branch edge 和条件边 gate；review 和持久化
的对象始终是 canonical `DAGSpec`，
而不是 source。

```python
agent = dagent.DagAgent(
    planner_profile="dag_agent",
    capabilities=["tool.search"],
    skills=["research/briefing"],
    max_steps=6,
    review="careful",
    dynamic_adjust=True,
)

result = await runner.run(
    agent,
    input="Research 达智 and write a note.",
)

if result.requires_review and result.review is not None:
    result = await runner.resume(result.review.approve())
```

如果希望 planner 只生成初始 DAG，之后按固定 DAG 执行，可设置
`dynamic_adjust=False`。`review` 逻辑保持不变；关闭动态调整只会禁止根据执行观察或失败
进行后续 replan。

用于 `DagAgent` 的 custom provider 必须在 `chat(...)` 和 `stream_chat(...)` 中实现
`response_format` keyword，并返回符合 system prompt 中 compact JSON Schema 的对象。
内置 `Provider` 使用 OpenAI-compatible Chat Completions 的 `json_object` response format。

运行离线 dynamic DAG 示例：

```bash
uv run python -m examples.dynamic_dag_agent
uv run python -m examples.dynamic_dag_builder_agent
```

## 子 Agent 委派

顶层 `ToolAgent`、`AutoAgent` 和 `DagAgent` run 可以把已注册的 `ToolAgent` 子 agent
暴露为 `agent.*` capabilities。子 agent 是叶子 agent：它可以使用自己配置好的 tools、
MCP capabilities 和 skills，但不能再调用另一个子 agent。已注册子 agent 必须使用
`review="fast"`；顶层 agent 负责 delegated call 的 review 行为。

```python
helper = dagent.ToolAgent(
    profile="conversation",
    name="helper",
    capabilities=["tool.search"],
    skills=["research/briefing"],
    max_steps=4,
    description="Research helper.",
)

runner.add_agent(helper)

agent = dagent.DagAgent(
    capabilities=["tool.read_file"],
    agents=["agent.helper"],
)
```

也可以直接在 `agents=[helper]` 中传入 `ToolAgent` 对象；runner 会在 run 前注册它。
使用 `agents="registered"` 会暴露该 runner 上已注册的全部 agent。`capabilities=None`
默认仍会排除 `agent.*` capabilities；只有设置 `agents=...` 或显式包含 `agent.*`
capability id 时，顶层 run 才能委派。

Dynamic DAG planner 会在 Available Tools section 中看到已暴露的 agent，并像调用其他
function 一样调用它们，通常只需要传 `prompt="..."`，必要时再传
`reference_content="..."`。子 `ToolAgent.max_steps` 声明是它的硬性局部上限，调用方不能
覆盖。非空参考内容会作为 task data 放入独立的 user-message 区块。仍包含调用级
`max_steps` 的旧持久化 DAG 可以继续执行，但该值会被忽略，并发出
`DeprecationWarning`。

## 共享 Agent 字段

| 字段 | 含义 |
| --- | --- |
| `profile` | Tool-loop system prompt，可以是内置名称、用户 profile 名称或 `AgentProfile`。 |
| `planner_profile` | `AutoAgent` 和 `DagAgent` 使用的 dynamic DAG planner profile。 |
| `capabilities` | 对 agent 可见的 capability ids 或 `@dagent.tool` bindings。 |
| `skills` | 在 tool-loop prompt 中建立索引，并可通过 `skill.list` 和 `skill.view` 读取的具体 skills。 |
| `agents` | 顶层 run 可见的子 agent capabilities：`None`、`"registered"`、`ToolAgent` 对象或 `agent.<name>` ids。 |
| `review` | risky work 的 review level。 |
| `max_steps` | 所有 agent 的局部执行上限，默认 `888`；对 `ToolAgent` 统计 tool-loop iterations，对 `DagAgent` 统计 dynamic DAG cycles，对 `AutoAgent` 统计实际选中执行引擎的 steps。 |
| `dynamic_adjust` | `AutoAgent` 和 `DagAgent` 生成初始 DAG 后是否允许继续动态 replan，默认 `True`。 |

Provider 重试、结果校验重试和上下文压缩不消耗 agent steps；如果它们实际触发了 model
或 capability 调用，仍会记录在累计 `ExecutionUsage` telemetry 中。`Runner` 不再有单独的
run-wide step limit。

Skill 索引遵循每个 tool agent 最终解析出的 scope，因此顶层会话和注册子 agent 可以暴露
不同技能，且不会互相泄漏 prompt。加载规则、prompt 预算和缓存稳定性见
[Skills](skills.md)。

传入 `capabilities=None` 会使用 runner 默认可见 capabilities。传入显式列表会将 agent
限制到该集合。

## 对话继续

Agent run 接收一个新的 `input` 和可选的、有界且与 provider 无关的
`ConversationState`：

```python
result = await runner.run(
    agent,
    input="Continue with one more detail.",
    conversation=result.conversation,
)
```

持久化和 review-safe resume flow 见[结果、流式输出和 Review](results-streaming-review.md)。
