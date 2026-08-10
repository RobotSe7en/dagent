# 静态 DAGs

当图结构在代码中已知时，使用 `Dag`。静态 DAG 是可序列化、可 review、类型化的。它们
使用显式边、结构化 value references、artifact 声明和有边界的控制流。

## 最小 DAG

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
    render_node = dagent.Node("render", target=render, inputs={"result": search_node.output})

    dag.add_node(search_node)
    dag.add_node(render_node)
    dag.add_edge(search_node, render_node)
    dag.output = render_node.output

    dagent.validate_dag_spec(dag.to_dag_spec())

    result = await runner.run(dag, graph_input="dagent")
    print(result.output_text)


asyncio.run(main())
```

运行 artifact 示例：

```bash
uv run python -m examples.static_dag
```

## 类型化 Input 和 Output

Pydantic graph inputs 和 Pydantic tool return values 是类型化参数传递的推荐路径：

```python
from pydantic import BaseModel


class ResearchInput(BaseModel):
    query: str
    audience: str = "engineers"


class SearchResult(BaseModel):
    title: str
    url: str


@dagent.tool
def search(q: str) -> SearchResult:
    return SearchResult(title=f"found:{q}", url="https://example.test")


dag = dagent.Dag("research", input=ResearchInput)
found = dagent.Node("search", target=search, inputs={"q": dag.input.query})
```

运行时 input 会在 path lookup 前转换成 JSON-like data：

```python
await runner.run(dag, graph_input=ResearchInput(query="dagent"))
```

`input_schema` 是一个 self-contained JSON Schema Draft 2020-12 文档。需要单独验证
实例时，可以调用：

```python
dagent.validate_dag_input(dag.to_dag_spec(), {"query": "dagent"})
```

`Runner` 会在创建 run workspace、执行 capability 或发送 `run.started` 之前执行相同
检查。无效实例会抛出 `DAGInputValidationError`。验证不会修改输入，也不会应用 schema
default。SDK 支持顶层 scalar 和 array schema，也支持引用同一 schema 文档中内嵌的资源。
验证 Pydantic model instance 时会按字段 alias dump，从而与 Pydantic 生成的 schema 对齐。

Subgraph 会在开始 child execution 前验证 resolved input。Loop body 会在每次迭代前执行
相同验证，包括上一轮返回并传入下一轮的 value。内嵌 input 不合法时，所属 node 会在任何
child capability call 之前失败。

把 value expression 赋给 `dag.output` 即可暴露结构化结果：

```python
dag.output = {
    "title": found.output.title,
    "url": found.output.url,
}

result = await runner.run(dag, graph_input={"query": "dagent"})
print(result.output_value)  # {"title": "...", "url": "..."}
```

对于静态 `Dag` 和 `DAGSpec` run，`output_value` 是精确解析得到的 scalar、list 或
object。为保持兼容，`output_text` 继续使用原有的文本/JSON rendering；其他 run kind 的
`output_value` 为 `None`。

## Value References

静态 DAG 参数可以包含 value references。它们会序列化为 `DAGSpec` 中的结构化 `$expr`
bindings，并在 capability call 前立即解析。

| SDK expression | Runtime value |
| --- | --- |
| `dag.input` | 整个 `Runner.run(dag, graph_input=...)` value |
| `dag.input.query` | `input["query"]` |
| `dag.input["query"]` | `input["query"]` |
| `node.output` | 上游节点 `CapabilityResult.value` |
| `node.output.title` | `value["title"]` |
| `node.output["title"]` | `value["title"]` |
| `node.content` | 上游节点 text content |
| `node.status` | 上游节点 status |
| `node.steps` | 上游节点 step count |
| `artifact.path` | 第一个相对 capability workspace 的 artifact path，可直接作为 tool 参数 |
| `artifact.paths` | 所有相对 capability workspace 的 artifact paths，可直接作为 tool 参数 |
| `artifact.absolute_path` | 在 run workspace 内解析出的第一个 artifact path |
| `artifact.absolute_paths` | 在 run workspace 内解析出的所有 artifact paths |
| `dag.format("Use {x}", x=node.output)` | nested refs 解析后的 format string |
| `node.output.score >= 0.8` | runtime comparison (`==`, `!=`, `<`, `<=`, `>`, `>=`) |
| `dagent.item` / `dagent.item.url` | 当前 map element，或 loop condition 中的最新 loop output |

在本地 Web UI 中，静态编排的变量选择器会展开 `tool.*` 和 `mcp.*`
capability 的顶层 `output_schema.properties`。例如结构化工具结果
`{"title": "...", "url": "..."}` 可以选择为 `search.output.title` 或
`search.output.url`。没有 output schema 的 capability 仍只暴露整体
`node.output`。

Web UI 的字符串参数也支持双花括号模板。切换到模板模式后，使用“插入变量”选择 graph
input、节点输出或 artifact；编辑器会在光标位置插入占位符，并直接绑定选中的来源：

```text
问题：{{ query }}
参考：{{ search_output }}
```

编辑器会把这段文本编译为现有的结构化 `format` expression。也可以手动输入
`{{ variable }}`，但编辑器不会根据名称推断来源：手动输入的占位符会显示变量选择器，并在
完成绑定前阻止保存和运行。选择上游输出时也会添加显式 dependency edge。生成的占位符
发生重名时会添加数字后缀，例如 `query_2`。

普通单花括号保持字面量，因此可以直接粘贴 JSON；需要原样输出 `{{ token }}` 时，在双花括号
前加 `\`，即使模板没有变量也会消费该转义。已有 Python format expression 如果包含 format
spec、索引字段或其他无法通过可视化语法无损往返的形式，Web UI 会原样保留并以只读方式展示；
这类表达式可在 Raw 模式中编辑。

SDK value references 不会创建 edges。Web 模板编辑器会为了编辑便利自动添加 edge；
在 Python 中仍需显式添加 dependencies：

```python
dag.add_node(found)
dag.add_node(rendered)
dag.add_edge(found, rendered)
```

当节点读取 non-upstream node、引用 unknown artifact 或使用 malformed expression 时，
validation 会 fail closed。

## Agent 节点

Python 中的静态 DAG 节点可以直接 target `ToolAgent` 对象。在本地 Web UI 中，受管
agent profiles 会暴露为 `agent.<name>` capabilities。例如
`~/.dagent/profiles/analyst.md` 会显示为 `agent.analyst`；选择它会创建一个 agent 节点，
它的 `prompt` 参数可以是固定值，也可以绑定 graph input、artifacts 或上游节点输出。
Agent 节点还会暴露可选的 `reference_content` 和 `max_steps` 参数。
`reference_content` 支持同样的 value references，因此检索节点可以直接把参考内容传给
agent，无需手动组装 format expression：

```python
retrieve_node = dagent.Node("retrieve", target=retrieve, inputs={"query": dag.input})
answer_node = dagent.Node(
    "answer",
    target=writer,
    inputs={
        "prompt": dag.input,
        "reference_content": retrieve_node.output,
    },
)
dag.add_node(retrieve_node)
dag.add_node(answer_node)
dag.add_edge(retrieve_node, answer_node)
```

运行时，非空参考内容会作为 task data 追加到 user message 的独立
`Reference content` 区块；参考内容为空时不会增加该区块或相关指令。`max_steps` 用于控制
内部有界 tool loop。完整示例可运行 `uv run python -m examples.static_rag`。

Web UI 的 agent 节点能力范围会映射到公开 SDK 字段：
`ToolAgent(capabilities=[...], skills=[...])`。Tool 和 MCP 选择会保存为
`tool.search`、`mcp.browser.open` 这类 capability ids；Skill 选择会保存为 skill names。
范围保持“全部”时使用外层 `Runner` 的默认可见能力。provider、workspace root、profile
存储以及 MCP/Skill 的注册来源仍由外层 `Runner` 和 API 配置拥有。

## Artifacts 和 Boundaries

Artifacts 声明 DAG 产生或消费的文件。Boundaries 约束副作用。对于内置的
path-aware tools，传入 `artifact.path`；如果自定义 Python tool 直接消费文件系统路径，
通常应使用 `artifact.absolute_path`：

```python
report = dag.artifact("report", "outputs/report.md")

write_node = dagent.Node(
    "write_report",
    target=write_note,
    inputs={"path": report.absolute_path, "content": search_node.output},
    artifact_outputs=[report],
    boundary=dagent.Boundary(
        allowed_paths=[report.absolute_path.as_expr()],
    ),
)
```

Artifact path escape 会被 runtime boundary checks 拒绝。

## Conditional Edges

`add_edge(..., when=...)` 根据 runtime condition 控制一条 edge。Reference comparisons
会构造 condition；普通 reference 会按 truthiness 测试。

```python
score_node = dagent.Node("score", target=score, inputs={"text": dag.input})
publish_node = dagent.Node("publish", target=publish, inputs={"content": dag.input})
revise_node = dagent.Node("revise", target=revise, inputs={"content": dag.input})

dag.add_edge(score_node, publish_node, when=score_node.output["score"] >= 0.8)
dag.add_edge(score_node, revise_node, when=score_node.output["score"] < 0.8)
```

如果一个节点所有 live incoming edges 都失败，它会被标记为 `skipped`，并且 skip 会向下游
cascade。读取 skipped node 的 output 会解析为 `None`；`node.status` 会解析为
`"skipped"`。

在 WebUI 静态 DAG 编辑器中，选择一条连线即可打开边检查器。连线可以设为无条件依赖、
变量真值判断，或者与字面量/另一个可用变量进行比较。变量选择器只提供 graph input、
artifacts 及目标节点的上游节点，因此条件不会读取到边求值时尚不可用的输出。
条件边会使用不同颜色，并在静态、动态和审核画布上显示简短的 `IF ...` 标签。超出当前
可视化编辑器支持范围的表达式仍会显示，并以只读 JSON 原样保留。

条件控制的是单条入边，而不是全局控制目标节点。目标节点会等待所有入边完成判断，只要
至少一条入边仍然成立就会执行。因此，如果目标节点同时存在无条件入边，另一条条件入边
可能不会起到阻断作用；边检查器会对此给出提示。

## Map Fan-Out

`MapNode` 会把一次 capability call fan out 到 runtime 解析出的列表上。`over` 必须解析为
list；`inputs` 可以通过 `dagent.item` 引用当前元素。

```python
fetch_all = dagent.MapNode(
    "fetch_all",
    target=fetch,
    over=search_node.output.urls,
    inputs={"url": dagent.item},
    max_items=64,
    max_concurrency=8,
)
```

节点 value 是按 item 顺序排列的 per-item values 列表。

## Subgraphs

当 `Node` 的 target 是一个 `Dag` 时，它会以 embedded subgraph 运行该图。`inputs` 会成为
子图的 graph input。

```python
def report_dag() -> dagent.Dag:
    sub = dagent.Dag("report", input=str)
    fetch_node = dagent.Node("fetch", target=fetch, inputs={"url": sub.input})
    publish_node = dagent.Node("publish", target=publish, inputs={"content": fetch_node.output})
    sub.add_node(fetch_node)
    sub.add_node(publish_node)
    sub.add_edge(fetch_node, publish_node)
    sub.output = publish_node.output
    return sub


report_node = dagent.Node("make_report", target=report_dag(), inputs=dag.input)
```

Embedded specs 会和 parent 一起递归 validation。

## Loops

`LoopNode` 会重复运行一个 `Dag` body，直到 `until` 为 truthy，最多运行
`max_iterations` 次。每次 iteration 的 output 会作为下一次 iteration 的 graph input。

```python
refine = dagent.LoopNode(
    "refine",
    body=refine_dag(),
    until=dagent.item["score"] >= 0.9,
    max_iterations=3,
    input=draft_node.output,
)
```

`max_iterations` 是必填项，这样 reviewers 在批准计划前能看到静态成本上限。静态 DAG
没有无界循环。

运行完整控制流示例：

```bash
uv run python -m examples.control_flow
```
