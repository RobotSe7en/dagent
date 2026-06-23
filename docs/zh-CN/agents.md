# Agents

dagent 有三种公开 agent 配置：`ToolAgent`、`AutoAgent` 和 `DagAgent`。它们都是声明式
run targets。`Runner` 拥有 provider clients、capabilities、runtime state、review
continuation 和 execution dispatch。

## 选择 Agent

| Agent | 适用场景 |
| --- | --- |
| `ToolAgent` | 模型应使用有边界的 tool loop，并根据最新 observation 选择下一步动作。 |
| `AutoAgent` | runtime 应针对每个请求选择直接 tool use 或 dynamic DAG planning。 |
| `DagAgent` | 模型应规划可 review 的 DAG，执行 ready layers，观察结果，并进行局部 replan。 |

当图结构属于代码时，使用静态 `Dag` 而不是 agent。见[静态 DAG](static-dag.md)。

## 本地 Web UI 的受管 Profiles

内置 profiles 位于 `dagent/resources/profiles/*.md`。本地 FastAPI/Web UI 会把可编辑
profiles 管理在 `~/.dagent/profiles/<name>.md` 下；用户通过“智能体管理”工作区创建、
复制、编辑和删除这些 profiles，而不是在界面里填写 Markdown 文件路径。

受管 profile 名称也是 agent capability 的产品标识，必须以字母开头，并且只能包含字母、
数字、`_` 和 `-`。名为 `analyst` 的受管 profile 会在静态 DAG 编辑器中暴露为
`agent.analyst`。

## ToolAgent

```python
import asyncio

import dagent


@dagent.tool
def echo(text: str) -> str:
    return f"echo:{text}"


async def main():
    runner = dagent.Runner(provider=provider, workspace=".dagent", capabilities=[echo])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["tool.echo"],
        skills=["writing/terse"],
        max_steps=8,
        review="fast",
    )

    result = await runner.run(
        agent,
        messages=[{"role": "user", "content": "Use echo to respond with hello."}],
    )
    print(result.output_text)
    runner.close()


asyncio.run(main())
```

运行离线示例：

```bash
uv run python -m examples.tool_agent
```

## AutoAgent

`AutoAgent` 没有 mode 字段。它会为每个请求路由到直接 tool use 或 dynamic DAG planning。

```python
agent = dagent.AutoAgent(
    profile="conversation",
    planner_profile="dag_agent",
    capabilities=["tool.search"],
    skills=["research/briefing"],
    max_steps=8,
    max_cycles=6,
    review="fast",
    dynamic_adjust=True,
)

messages = [{"role": "user", "content": "Answer directly or plan if orchestration helps."}]
result = await runner.run(agent, messages=messages)
messages += result.messages
```

运行离线示例：

```bash
uv run python -m examples.auto_agent
```

## DagAgent

`DagAgent` 用于 dynamic DAG planning。它可以在执行 proposed work 前暂停等待 human review。

```python
agent = dagent.DagAgent(
    planner_profile="dag_agent",
    capabilities=["tool.search"],
    skills=["research/briefing"],
    max_cycles=6,
    review="careful",
    dynamic_adjust=True,
)

result = await runner.run(
    agent,
    messages=[{"role": "user", "content": "Research dagent and write a note."}],
)

if result.requires_review and result.review is not None:
    result = await runner.resume(result.review.approve())
```

如果希望 planner 只生成初始 DAG，之后按固定 DAG 执行，可设置
`dynamic_adjust=False`。`review` 逻辑保持不变；关闭动态调整只会禁止根据执行观察或失败
进行后续 replan。

运行离线 dynamic DAG 示例：

```bash
uv run python -m examples.dynamic_dag_agent
```

## 共享 Agent 字段

| 字段 | 含义 |
| --- | --- |
| `profile` | Tool-loop system prompt，可以是内置名称、用户 profile 名称或 `AgentProfile`。 |
| `planner_profile` | `AutoAgent` 和 `DagAgent` 使用的 dynamic DAG planner profile。 |
| `capabilities` | 对 agent 可见的 capability ids 或 `@dagent.tool` bindings。 |
| `skills` | 通过 `skill.list` 和 `skill.view` 可见的具体 skills。 |
| `review` | risky work 的 review level。 |
| `max_steps` | `ToolAgent` 和 `AutoAgent` 的 tool-loop bound。 |
| `max_cycles` | `AutoAgent` 和 `DagAgent` 的 dynamic DAG replan bound。 |
| `dynamic_adjust` | `AutoAgent` 和 `DagAgent` 生成初始 DAG 后是否允许继续动态 replan，默认 `True`。 |

传入 `capabilities=None` 会使用 runner 默认可见 capabilities。传入显式列表会将 agent
限制到该集合。

## 对话继续

Agent runs 接收 OpenAI-compatible `messages`。结果只包含当前 run 生成的 messages，
因此继续前需要追加它们：

```python
messages += result.messages
messages.append({"role": "user", "content": "Continue with one more detail."})
result = await runner.run(agent, messages=messages, state=result.state)
```

持久化和 review-safe resume flow 见[结果、流式输出和 Review](results-streaming-review.md)。
