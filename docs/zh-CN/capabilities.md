# Capabilities

Capabilities 是注册到 `Runner` 的可执行动作。Agents 和 DAG nodes 不直接执行函数；
它们通过 runtime capability catalog 调用 capability ids。

## Capability Ids

| 来源 | Id 格式 |
| --- | --- |
| Python function tools | `tool.<name>` |
| MCP stdio tools | `mcp.<server>.<tool>` |
| 内置 skill accessors | `skill.list`, `skill.view` |

Capability ids 是公开行为。不要依赖这里未记录的 legacy aliases。

## Python Function Tools

用 `@dagent.tool` 装饰 Python 函数。参数注解会生成 tool input JSON schema；返回注解会
生成 output schema。

```python
from pydantic import BaseModel

import dagent


class SearchResult(BaseModel):
    title: str
    url: str


@dagent.tool
def search(q: str) -> SearchResult:
    return SearchResult(title=f"found:{q}", url="https://example.test")
```

可以在构造时或之后注册 tools：

```python
runner = dagent.Runner(provider=provider, capabilities=[search])
runner.add_tool(search)
```

Agents 声明自己能使用什么：

```python
agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["tool.search"],
)
```

## 结构化结果

普通 `str`、`dict`、`list`、数字、布尔值、tuple、bytes 和 Pydantic models 会被转换成
`CapabilityResult.content` 和 `CapabilityResult.value`。静态 DAG 节点输出引用默认从
`value` 读取。

如果工具直接返回 `CapabilityResult`，且 completed result 没有显式 `value`，则使用
`content` 作为 value。

## Tool Context 和 Boundaries

需要 run workspace 或 callbacks 的工具可以选择接收 runtime context：

```python
from pathlib import Path

import dagent


@dagent.tool(risk="medium", supports_context=True)
def write_note(path: str, content: str, *, context, callbacks=None) -> str:
    resolved = Path(context.workspace_path) / path
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"wrote:{path}"
```

DAG nodes 可以为有副作用的工作传入 boundaries：

```python
report = dag.artifact("report", "outputs/report.md")

write_node = dagent.Node(
    "write_report",
    target=write_note,
    inputs={"path": report.path, "content": search_node.output},
    artifact_outputs=[report],
    boundary=dagent.Boundary(
        mode="write_limited",
        allowed_paths=[report.path.as_expr()],
    ),
)
```

Boundary modes 包括 `read_only`、`write_limited` 和 `full`。Boundary values 可以是字面量
字符串，也可以是结构化 value references。

## Capability Policies

`CapabilityPolicy` 记录 risk 和执行要求：

```python
policy = dagent.CapabilityPolicy(
    risk="medium",
    requires_review=True,
    network=False,
    secrets=[],
)
```

Agents 和 runs 上的 review 设置决定 medium/high-risk 工作什么时候暂停等待批准。

## MCP Tools

MCP stdio server tools 在 server 注册后会变成普通 `mcp.<server>.<tool>` capabilities：

```python
runner.add_mcp_server(
    "fs",
    {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
    },
)

agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["mcp.fs.read_file"],
)
```

注册 MCP servers 前先安装 MCP extra：

```bash
pip install "dagent-ai[mcp]"
```

动态 MCP 注册和替换见 [Runner 和配置](runner-and-configuration.md)。

## 直接测试 Capability

使用 `Runner.test_capability(...)` 单独执行一个 capability 进行检查：

```python
result = await runner.test_capability("tool.search", {"q": "dagent"})
print(result.status)
print(result.value)
```
