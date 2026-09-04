# 快速开始

本指南给出达智的第一条完整路径：安装包、注册 Python 工具、运行
`ToolAgent`、构建一个很小的静态 DAG，再从仓库 checkout 启动终端界面。

## 1. 安装达智

```bash
pip install dagent-ai
```

只有在需要注册 MCP server 时才需要 MCP extra：

```bash
pip install "dagent-ai[mcp]"
```

## 2. 配置 Provider

达智面向 OpenAI-compatible `/v1/chat/completions` provider：

```python
import dagent


provider = dagent.Provider(
    base_url="https://api.openai.com/v1",
    model="your-model",
    api_key_env="OPENAI_API_KEY",
)
```

Provider-specific 选项属于 provider，而不是 agent 对象。对于支持 reasoning 的
endpoint，直接配置协议无关的推理强度：

```python
provider = dagent.Provider(
    base_url="https://api.deepseek.com",
    model="deepseek-v4-pro",
    api_key_env="DEEPSEEK_API_KEY",
    reasoning_effort="high",
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
    runner = dagent.Runner(provider=provider, capabilities=[echo])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["tool.echo"],
    )

    result = await runner.run(
        agent,
        input="Use echo to respond with hello.",
    )

    print(result.status)
    print(result.output_text)
    runner.close()


asyncio.run(main())
```

`Runner` 默认把 workspace 放在 `~/.dagent`，并使用相对的 `.runtime` 作为私有运行目录。
自行管理存储位置的应用可以显式覆盖其中任意一个值。

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

    runner = dagent.Runner(provider=provider)
    result = await runner.run(dag, graph_input="达智")
    print(result.output_text)
    runner.close()


asyncio.run(main())
```

带 artifact 输出的离线静态 DAG 示例：

```bash
uv run python -m examples.static_dag
```

## 6. 使用终端界面

仓库包含基于 Textual 的 `dagent-tui`，它直接运行在终端中，不是浏览器应用。TUI
通过 HTTP 和 SSE 请求连接本地 FastAPI 后端；后端使用达智 SDK，并负责会话、运行
状态、Review 恢复和持久化。

这个流程需要仓库 checkout、Python 3.11 或更新版本，以及
[`uv`](https://docs.astral.sh/uv/)。先在根目录的 `config.yaml` 中配置 provider，并导出
其中 `api_key_env` 指定的环境变量。当前仓库配置使用 `API_KEY`：

```bash
export API_KEY="你的 provider key"
```

在仓库根目录的第一个终端中启动 API：

```bash
uv run --extra dev uvicorn api.app:app --port 8001
```

在第二个终端中启动 TUI：

```bash
uv run --project tui dagent-tui --api-url http://127.0.0.1:8001
```

`http://127.0.0.1:8001` 是 TUI 使用的后端地址，不需要在浏览器中打开。也可以通过
环境变量设置该地址：

```bash
export DAGENT_API_URL="http://127.0.0.1:8001"
uv run --project tui dagent-tui
```

在输入框上方选择 `Auto`、`Tool` 或 `DAG` 运行目标，并选择 `Fast review` 或
`Careful review`。左侧显示持久化会话，中间是对话区，右侧显示 capability 活动以及
DAG 和 trace 摘要。

| 按键 | 操作 |
| --- | --- |
| `Ctrl+N` | 新建独立会话 |
| `Ctrl+R` | 重试上一条请求 |
| `Ctrl+C` | 取消当前运行 |
| `F5` | 刷新项目和会话 |
| `Ctrl+Q` | 退出 |

当前 TUI 可以继续已有项目会话，但新建的会话是独立会话。图形编辑、文件上传、富
artifact 预览以及 provider、MCP 和 skill 管理仍由 WebUI 提供。当前功能范围和限制见
[TUI 使用说明](../../tui/README.md)。

## 接下来读什么

- 在[核心概念](concepts.md)中理解达智的模型。
- 在 [Agents](agents.md) 中选择 `ToolAgent`、`AutoAgent` 或 `DagAgent`。
- 在 [Capabilities](capabilities.md) 中注册 Python tools、MCP tools 和结构化结果。
- 在[静态 DAG](static-dag.md) 中构建类型化静态 DAG。
- 在[结果、流式输出和 Review](results-streaming-review.md) 中继续运行、构建
  streaming UI 和处理 review。
