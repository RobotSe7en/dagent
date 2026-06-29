# Static DAGs

Use `Dag` when the graph shape is known in code. Static DAGs are serializable,
reviewable, and typed. They use explicit edges, structured value references,
artifact declarations, and bounded control flow.

## Minimal DAG

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

Run the artifact example:

```bash
uv run python -m examples.static_dag
```

## Typed Input and Output

Pydantic graph inputs and Pydantic tool return values are the preferred path for
typed parameter passing:

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

Runtime input is converted to JSON-like data before path lookup:

```python
await runner.run(dag, graph_input=ResearchInput(query="dagent"))
```

## Value References

Static DAG arguments can contain value references. They serialize as structured
`$expr` bindings in `DAGSpec` and resolve immediately before capability calls.

| SDK expression | Runtime value |
| --- | --- |
| `dag.input` | Whole `Runner.run(dag, graph_input=...)` value |
| `dag.input.query` | `input["query"]` |
| `dag.input["query"]` | `input["query"]` |
| `node.output` | Previous node `CapabilityResult.value` |
| `node.output.title` | `value["title"]` |
| `node.output["title"]` | `value["title"]` |
| `node.content` | Previous node text content |
| `node.status` | Previous node status |
| `node.steps` | Previous node step count |
| `artifact.path` | First artifact path relative to the capability workspace, for tool arguments |
| `artifact.paths` | All artifact paths relative to the capability workspace, for tool arguments |
| `artifact.absolute_path` | First artifact path resolved inside the run workspace |
| `artifact.absolute_paths` | All artifact paths resolved inside the run workspace |
| `dag.format("Use {x}", x=node.output)` | Format string after nested refs resolve |
| `node.output.score >= 0.8` | Runtime comparison (`==`, `!=`, `<`, `<=`, `>`, `>=`) |
| `dagent.item` / `dagent.item.url` | Current map element, or latest loop output in loop conditions |

In the local Web UI, the static orchestration variable picker expands top-level
`output_schema.properties` for `tool.*` and `mcp.*` capabilities, so a structured
tool result such as `{"title": "...", "url": "..."}` can be selected as
`search.output.title` or `search.output.url`. Capabilities without an output
schema still expose the whole `node.output` value.

References do not create edges. Add dependencies explicitly:

```python
dag.add_node(found)
dag.add_node(rendered)
dag.add_edge(found, rendered)
```

Validation fails closed when a node reads from a non-upstream node, references an
unknown artifact, or uses a malformed expression.

## Agent Nodes

Static DAG nodes can target `ToolAgent` objects in Python. In the local Web UI,
managed agent profiles are exposed as `agent.<name>` capabilities. For example,
the profile `~/.dagent/profiles/analyst.md` appears as `agent.analyst`; selecting
it creates an agent node whose `prompt` argument can be a fixed value or a
structured value reference from graph input, artifacts, or upstream nodes.
Agent nodes also expose `max_steps` for the bounded inner tool loop.

The Web UI maps agent-node capability controls onto the public SDK fields:
`ToolAgent(capabilities=[...], skills=[...])`. Tool and MCP selections become
capability ids such as `tool.search` or `mcp.browser.open`; skill selections
become skill names. Leaving the scope on "All" uses the surrounding `Runner`'s
default visible capabilities. Provider, workspace root, profile storage, and
MCP/skill registration are still owned by the surrounding `Runner` and API
configuration.

## Artifacts and Boundaries

Artifacts declare files produced or consumed by the DAG. Boundaries constrain
side effects. For built-in path-aware tools, pass `artifact.path`; custom
Python tools that consume raw filesystem paths should usually use
`artifact.absolute_path`:

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

Artifact path escapes are rejected by runtime boundary checks.

## Conditional Edges

`add_edge(..., when=...)` gates an edge on a runtime condition. Reference
comparisons build the condition; a plain reference is tested for truthiness.

```python
score_node = dagent.Node("score", target=score, inputs={"text": dag.input})
publish_node = dagent.Node("publish", target=publish, inputs={"content": dag.input})
revise_node = dagent.Node("revise", target=revise, inputs={"content": dag.input})

dag.add_edge(score_node, publish_node, when=score_node.output["score"] >= 0.8)
dag.add_edge(score_node, revise_node, when=score_node.output["score"] < 0.8)
```

A node whose live incoming edges all fail is marked `skipped`, and skips cascade
downstream. Reading a skipped node's output resolves to `None`; `node.status`
resolves to `"skipped"`.

## Map Fan-Out

`MapNode` fans one capability call out over a list resolved at runtime. `over`
must resolve to a list; `inputs` can reference the current element through
`dagent.item`.

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

The node value is the list of per-item values in item order.

## Subgraphs

A `Node` whose target is a `Dag` runs that graph as an embedded subgraph.
`inputs` becomes the child graph input.

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

Embedded specs are validated recursively with the parent.

## Loops

`LoopNode` runs a `Dag` body repeatedly until `until` is truthy, at most
`max_iterations` times. Each iteration's output feeds the next iteration's graph
input.

```python
refine = dagent.LoopNode(
    "refine",
    body=refine_dag(),
    until=dagent.item["score"] >= 0.9,
    max_iterations=3,
    input=draft_node.output,
)
```

`max_iterations` is mandatory so reviewers see a static cost bound before
approving the plan. There is no unbounded loop in static DAGs.

Run the full control-flow example:

```bash
uv run python -m examples.control_flow
```
