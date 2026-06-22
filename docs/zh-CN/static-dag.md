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

References 不会创建 edges。需要显式添加 dependencies：

```python
dag.add_node(found)
dag.add_node(rendered)
dag.add_edge(found, rendered)
```

当节点读取 non-upstream node、引用 unknown artifact 或使用 malformed expression 时，
validation 会 fail closed。

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
