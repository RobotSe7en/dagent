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

## Agent-node tool review

A direct `Node(..., target=ToolAgent(...))` can pause a static DAG when its
inner tool call needs review. Pass the usual run-level policy to `Runner.run`:

```python
result = await runner.run(dag, review="careful")
if result.requires_review:
    result = await runner.resume(result.review.approve(), checkpoint=result.checkpoint)
```

With `careful`, the agent's medium- and high-risk inner tools pause for review.
At either level, an inner tool that exceeds its node boundary can pause for a
one-invocation boundary override. Approval or rejection resumes the same
`ToolAgent` conversation; rejection feeds the decision back to the model and
does not execute the tool.

The DAG author already authorizes ordinary capability nodes, including
high-risk ones, so they still execute directly. This continuation supports only
top-level direct agent capability nodes. An agent in a `MapNode`, `Subgraph`,
or `LoopNode` is rejected before execution because those nested progress states
are not resumable yet; a registered agent is also a leaf and cannot expose
another agent. Persist `result.checkpoint` and restore it with a compatible
Runner just as for other review continuations. Its direct agent-node execution
configuration is fingerprinted, so a changed profile, step limit, policy,
skills, or inner tool scope is rejected rather than silently changing a
resumed run.

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

`input_schema` is a self-contained JSON Schema Draft 2020-12 document. Validate
an instance independently when needed:

```python
dagent.validate_dag_input(dag.to_dag_spec(), {"query": "dagent"})
```

`Runner` performs the same check before creating a run workspace, executing a
capability, or emitting `run.started`. Invalid instances raise
`DAGInputValidationError`. Validation does not mutate the value or apply schema
defaults. Scalar and array top-level schemas are supported, as are references
to resources embedded in the same schema document. Pydantic model instances are
dumped with field aliases for validation so they match Pydantic's generated
schema.

Subgraphs validate their resolved input before starting child execution. Loop
bodies perform the same validation before every iteration, including values
returned by the previous iteration. An invalid embedded input fails the owning
node before any child capability is called.

Assign `dag.output` to a value expression to expose a structured result:

```python
dag.output = {
    "title": found.output.title,
    "url": found.output.url,
}

result = await runner.run(dag, graph_input={"query": "dagent"})
print(result.output_value)  # {"title": "...", "url": "..."}
```

For static `Dag` and `DAGSpec` runs, `output_value` is the exact resolved
scalar, list, or object. `output_text` retains its existing text/JSON rendering
for compatibility. Other run kinds leave `output_value` as `None`.

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

String arguments in the Web UI also support double-brace templates. Switch to
template mode and use **Insert variable** to choose a graph input, node output,
or artifact. The editor inserts a placeholder at the cursor and binds the
selected source directly:

```text
Question: {{ query }}
Evidence: {{ search_output }}
```

The editor compiles that text to the existing structured `format` expression.
You can also type `{{ variable }}` manually, but the editor does not infer its
source from the name: manually entered placeholders stay visible with a variable
selector and block save/run until they are bound. Selecting an upstream output
also adds the explicit dependency edge. Repeated generated names receive a
numeric suffix such as `query_2`.

Ordinary single braces remain literal, so JSON can be pasted without escaping.
Prefix a double-brace token with `\` when the literal `{{ token }}` text is
required; the escape is consumed even when the template has no variables.
Existing Python format expressions that use format specs, indexed fields, or
another form that cannot round-trip through the visual syntax are preserved and
shown read-only. Edit those expressions in Raw mode.

SDK value references do not create edges. The Web template editor adds an edge
as an editing convenience; in Python, add dependencies explicitly:

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
Agent nodes also expose optional `reference_content` and `max_steps` arguments.
`reference_content` accepts the same value references, so a retrieval node can
feed evidence to the agent without assembling a format expression:

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

At runtime, non-empty reference content is appended to the user message in a
separate `Reference content` section and treated as task data. Empty reference
content adds no section or related instruction. `max_steps` bounds the inner
tool loop. Run the complete example with
`uv run python -m examples.static_rag`.

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

## Exclusive Condition Nodes

Use `ConditionNode` when the workflow must choose exactly one ordered
IF/ELIF/ELSE branch. Each `Case` declares a stable branch id and an expression;
the first truthy case wins, otherwise the required default branch wins.

```python
score_node = dagent.Node("score", target=score, inputs={"text": dag.input})
route_node = dagent.ConditionNode(
    "route",
    cases=[
        dagent.Case("high", score_node.output["score"] >= 0.8),
        dagent.Case("medium", score_node.output["score"] >= 0.5),
    ],
    default_branch="low",
)
publish_node = dagent.Node("publish", target=publish, inputs={"content": dag.input})
revise_node = dagent.Node("revise", target=revise, inputs={"content": dag.input})

dag.add_node(score_node)
dag.add_node(route_node)
dag.add_node(publish_node)
dag.add_node(revise_node)
dag.add_edge(score_node, route_node)
dag.add_edge(route_node, publish_node, branch="high")
dag.add_edge(route_node, revise_node, branch="medium")
# The unconnected "low" branch deliberately ends this path.
```

Condition output is `{"branch": "<selected id>"}` and its trace node exposes
the same choice as `selected_branch`. Multiple outgoing edges may use the same
branch id for deliberate fan-out. Outgoing edges from a condition node must use
`branch`; other nodes cannot emit branch edges. A branch edge cannot also use
`when`.

Compose structured boolean expressions with `all_of`, `any_of`, and `not_`:

```python
ready = dagent.all_of(
    score_node.output["score"] >= 0.8,
    dagent.not_(score_node.output["blocked"]),
)
route_node = dagent.ConditionNode(
    "route",
    cases=[dagent.Case("publish", ready)],
    default_branch="revise",
)
```

Expressions may read graph input, artifacts, or upstream node output through
the existing structured value bindings. A node-output read still requires an
explicit dependency edge; the SDK never infers graph edges from expressions.

The WebUI static and dynamic canvases expose one source handle per case plus an
ELSE handle. The condition inspector supports ordered case editing, stable
branch ids, branch rename propagation, and structured `ALL`/`ANY`/`NOT`
expressions.

## Conditional Edge Gates

`add_edge(..., when=...)` remains available for a simple independent gate on
one ordinary edge. Reference comparisons build the condition; a plain reference
is tested for truthiness.

```python
dag.add_edge(score_node, publish_node, when=score_node.output["score"] >= 0.8)
```

A node whose live incoming edges all fail is marked `skipped`, and skips cascade
downstream. Reading a skipped node's output resolves to `None`; `node.status`
resolves to `"skipped"`.

In the WebUI static-DAG editor, select an edge to open the edge inspector. An
edge can be changed between an unconditional dependency, a truthiness check,
and a comparison against a literal or another available variable. The picker
only offers graph input, artifacts, and nodes upstream of the target, so a
condition cannot read output that is unavailable when the edge is evaluated.
Conditional edge gates use a distinct color and carry a compact `IF ...`
label on static, dynamic, and review canvases. Expressions outside the visual
editor's supported subset remain visible and are preserved as read-only JSON.

`when` conditions gate individual incoming edges, not the target node globally. A
target waits for all incoming edges to settle and runs when at least one remains
live. In particular, an unconditional incoming edge can make another conditional
incoming edge irrelevant; the inspector warns about this arrangement.

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
