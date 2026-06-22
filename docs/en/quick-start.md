# Quick Start

This guide gives you a complete first path through dagent: install the package,
register a Python tool, run a `ToolAgent`, and build a tiny static DAG.

## 1. Install dagent

```bash
pip install dagent-ai
```

Use the MCP extra only when you need stdio MCP server registration:

```bash
pip install "dagent-ai[mcp]"
```

## 2. Configure a Provider

dagent talks to OpenAI-compatible `/v1/chat/completions` providers:

```python
import dagent


provider = dagent.Provider(
    base_url="https://api.openai.com/v1",
    model="your-model",
    api_key_env="OPENAI_API_KEY",
)
```

Provider-specific options belong on the provider, not on agent objects. For
reasoning-capable endpoints, use the common `reasoning` shortcut when it maps to
your provider:

```python
provider = dagent.Provider(
    base_url="https://api.deepseek.com",
    model="deepseek-v4-pro",
    api_key_env="DEEPSEEK_API_KEY",
    reasoning={"enabled": True, "effort": "high"},
)
```

## 3. Register a Python Tool

Python functions become capabilities with `tool.<name>` ids:

```python
@dagent.tool
def echo(text: str) -> str:
    return f"echo:{text}"
```

## 4. Run a ToolAgent

`Runner` owns runtime state and registered capabilities. `ToolAgent` is only
declarative configuration for one run target.

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
    runner = dagent.Runner(workspace=".dagent", provider=provider, capabilities=[echo])
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

For an offline version of this example using `MockProvider`, run:

```bash
uv run python -m examples.tool_agent
```

## 5. Build a Static DAG

Use `Dag` when the graph shape belongs in code. Node output references do not
create dependencies by themselves, so add explicit edges.

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

    runner = dagent.Runner(provider=provider, workspace=".dagent")
    result = await runner.run(dag, graph_input="dagent")
    print(result.output_text)
    runner.close()


asyncio.run(main())
```

For an offline static DAG with artifact output, run:

```bash
uv run python -m examples.static_dag
```

## Where to Go Next

- Learn the mental model in [Core Concepts](concepts.md).
- Choose between `ToolAgent`, `AutoAgent`, and `DagAgent` in
  [Agents](agents.md).
- Register Python tools, MCP tools, and structured results in
  [Capabilities](capabilities.md).
- Build typed static DAGs in [Static DAGs](static-dag.md).
- Continue runs and build streaming UIs with
  [Results, Streaming, and Review](results-streaming-review.md).
