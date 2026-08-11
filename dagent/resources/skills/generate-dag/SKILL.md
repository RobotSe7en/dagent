---
name: generate-dag
description: Generate complete dynamic dagent graphs as canonical Python Builder source when the planner is using the sdk_builder frontend, including dependencies, conditions, maps, loops, subgraphs, artifacts, and outputs.
---

# Generate a DAG

Return one complete graph as canonical dagent Builder source. The host parses the
source without executing Python and converts the root variable `dag` to a
`DAGSpec`.

## Write canonical source

- Do not write imports, functions, classes, branches, loops, comprehensions, or
  comments outside the source.
- Assign every graph, artifact, and node to a descriptive snake_case variable.
- Assign the root graph to the variable `dag`.
- Use `dagent.Dag`, `dagent.Node`, `dagent.ConditionNode`, `dagent.Case`,
  `dagent.MapNode`, `dagent.LoopNode`, boolean helpers, and `dagent.item` only.
  Do not construct tools, agents, boundaries, or providers.
- Use only stable capability IDs from the injected Capability Catalog as node
  targets. Business skills belong to registered agents and are not node targets.
- Add every node with `add_node` and every data dependency with `add_edge`.
- Set `dag.output` when the graph has a useful final value.

```python
dag = dagent.Dag("research", name="Research")
search = dagent.Node(
    "search",
    target="tool.search",
    inputs={"query": dag.input.request},
)
write = dagent.Node(
    "write",
    target="agent.writer",
    inputs={"prompt": dag.format("Write from {result}", result=search.output)},
)
dag.add_node(search)
dag.add_node(write)
dag.add_edge(search, write)
dag.output = write.output
```

## Use values and conditions

- Use `dag.input`, `node.output`, `node.content`, `node.status`, and `node.steps`
  with attribute or literal index paths.
- Use `dag.format(template, name=value)` for runtime string formatting.
- Use `==`, `!=`, `<`, `<=`, `>`, or `>=` to build conditions. Combine them
  with `dagent.all_of(...)`, `dagent.any_of(...)`, and `dagent.not_(...)`.
- References never create edges; add the corresponding edge explicitly.

```python
dag.add_edge(check, publish, when=check.output.score >= 0.8)
```

Use a `ConditionNode` for ordered, mutually exclusive IF/ELIF/ELSE routing.
Every outgoing edge from it uses `branch=...`; keep `when=...` for simple
independent edge gates only.

```python
route = dagent.ConditionNode(
    "route",
    cases=[
        dagent.Case("high", check.output.score >= 0.8),
        dagent.Case("medium", check.output.score >= 0.5),
    ],
    default_branch="low",
)
dag.add_node(route)
dag.add_edge(check, route)
dag.add_edge(route, high, branch="high")
dag.add_edge(route, medium, branch="medium")
dag.add_edge(route, low, branch="low")
```

## Use artifacts

Declare artifacts before nodes. Pass artifact references through arguments or
node `artifact_inputs` and `artifact_outputs`. Add an edge from each producer to
each consumer.

```python
report = dag.artifact("report", ["report.md"], description="Final report")
render = dagent.Node(
    "render",
    target="tool.write_file",
    inputs={"path": report.path, "content": draft.content},
    artifact_outputs=[report],
)
```

## Use bounded control flow

Use `MapNode` only for runtime lists and always provide positive bounds. Refer
to the current element with `dagent.item`.

```python
fanout = dagent.MapNode(
    "fetch_pages",
    target="mcp.browser.open",
    over=search.output.urls,
    inputs={"url": dagent.item},
    max_items=20,
    max_concurrency=4,
)
```

Build subgraphs as separate `Dag` values and target them from an ordinary
`Node`. Build bounded loops with a separate body graph and `dagent.item` in the
termination condition.

```python
body = dagent.Dag("refine_body")
refine = dagent.Node(
    "refine",
    target="agent.editor",
    inputs={"prompt": body.input},
)
body.add_node(refine)
body.output = refine.output

loop = dagent.LoopNode(
    "refine_until_ready",
    body=body,
    input=draft.output,
    until=dagent.item.score >= 0.9,
    max_iterations=3,
)
dag.add_node(loop)
dag.add_edge(draft, loop)
```

## Replan safely

- Return the complete graph, including unchanged completed and pending nodes.
- Preserve unchanged node IDs and semantics.
- Put a completed node ID in `rerun_nodes` only when it must execute again.
- Treat the current canonical DAG in the observation as authoritative if it
  differs from earlier Builder source.
- Return `no_change` when the pending graph remains correct, and
  `final_answer` only after work is complete or no graph is useful.

The host owns spec identity, versions, invocation IDs, capability kind, risk,
boundary, workspace, review, and runtime state. Never attempt to set them.
