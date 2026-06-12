# 快速开始

本指南给出 dagent 的第一条完整路径：安装包、注册 Python 工具、运行
`ToolAgent`，再构建一个很小的静态 DAG。

## 1. 安装 dagent

```bash
pip install dagent-ai
```

只有在需要注册 stdio MCP server 时才需要 MCP extra：

```bash
pip install "dagent-ai[mcp]"
```

## 2. 配置 Provider

dagent 面向 OpenAI-compatible `/v1/chat/completions` provider：

```python
import dagent


provider = dagent.Provider(
    base_url="https://api.openai.com/v1",
    model="your-model",
    api_key_env="OPENAI_API_KEY",
)
```

Provider-specific 选项属于 provider，而不是 agent 对象。对于支持 reasoning 的
endpoint，如果你的 provider 能映射这些字段，可以使用通用的 `reasoning` 快捷项：

```python
provider = dagent.Provider(
    base_url="https://api.deepseek.com",
    model="deepseek-v4-pro",
    api_key_env="DEEPSEEK_API_KEY",
    reasoning={"enabled": True, "effort": "high"},
)
```

## 3. 注册 Python Tool

Python 函数会变成 `tool.<name>` id 的 capability：

```python
@dagent.tool
def echo(text: str) -> str:
    return f"echo:{text}"
```

## 4. 运行 ToolAgent

`Runner` 拥有运行时状态和已注册 capabilities。`ToolAgent` 只是一次 run target 的
声明式配置。

```python
import asyncio

import dagent


@dagent.tool
def echo(text: str) -> str:
    return f"echo:{text}"


async def main():
    provider = dagent.Provider(
        base_url="https://api.openai.com/v1",
        model="your-model",
        api_key_env="OPENAI_API_KEY",
    )
    runner = dagent.Runner(workspace=".", provider=provider, capabilities=[echo])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["tool.echo"],
    )

    result = await runner.run(
        agent,
        messages=[{"role": "user", "content": "Use echo to respond with hello."}],
    )

    print(result.status)
    print(result.output_text)
    runner.close()


asyncio.run(main())
```

使用 `MockProvider` 的离线版本可以这样运行：

```bash
uv run python -m examples.tool_agent
```

## 5. 构建静态 DAG

当图结构应该写在代码里时，使用 `Dag`。节点输出引用不会自动创建依赖关系，因此需要
显式添加边。

```python
import asyncio

import dagent


@dagent.tool
def search(q: str) -> str:
    return f"found:{q}"


@dagent.tool
def render(result: str) -> str:
    return f"Report: {result}"


async def main():
    dag = dagent.Dag("research", input=str)
    search_node = dagent.Node("search", target=search, inputs={"q": dag.input})
    render_node = dagent.Node(
        "render",
        target=render,
        inputs={"result": search_node.output},
    )
    dag.add_node(search_node)
    dag.add_node(render_node)
    dag.add_edge(search_node, render_node)
    dag.output = render_node.output

    dagent.validate_dag_spec(dag.to_dag_spec())

    runner = dagent.Runner(provider=provider, workspace=".")
    result = await runner.run(dag, graph_input="dagent")
    print(result.output_text)
    runner.close()


asyncio.run(main())
```

带 artifact 输出的离线静态 DAG 示例：

```bash
uv run python -m examples.static_dag
```

## 接下来读什么

- 在[核心概念](concepts.md)中理解 dagent 的模型。
- 在 [Agents](agents.md) 中选择 `ToolAgent`、`AutoAgent` 或 `DagAgent`。
- 在 [Capabilities](capabilities.md) 中注册 Python tools、MCP tools 和结构化结果。
- 在[静态 DAG](static-dag.md) 中构建类型化静态 DAG。
- 在[结果、流式输出和 Review](results-streaming-review.md) 中继续运行、构建
  streaming UI 和处理 review。
