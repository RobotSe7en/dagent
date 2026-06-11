# Python SDK Guide

This guide covers the current public Python SDK. Runnable examples live in
[`examples/`](../examples/).

Install the PyPI package as `dagent-ai`; import it in Python as `dagent`.

## Public Surface

Most applications start with `Runner`, `@dagent.tool`, `AutoAgent`,
`ToolAgent`, `DagAgent`, `Dag`, and `SkillStore`.

| Area | Public SDK |
|------|------------|
| Runner and tools | `Runner`, `tool`, `CapabilityBinding` |
| Agents | `AutoAgent`, `ToolAgent`, `DagAgent` |
| Static DAGs | `Dag`, `Node`, `InputRef`, `NodeOutputRef`, `ArtifactRef`, `ArtifactValueRef`, `FormatRef`, `validate_dag_spec` |
| Profiles | `AgentProfile`, `ProfileStore`, `load_builtin_profile`, `list_builtin_profiles` |
| Skills | `SkillStore`, `SkillEntry`, `SkillView`, `SkillAmbiguousError`, `SkillNotFoundError`, `SkillPermissionError`, `SkillStoreError`, `default_skill_roots`, `default_managed_skill_root` |
| Reviews and results | `RunResult`, `RunState`, `RunStreamEvent`, `ReviewHandle`, `ReviewDecision`, `ReviewLevel` |
| Runtime schemas | `Boundary`, `CapabilityDefinition`, `CapabilityInvocation`, `CapabilityPolicy`, `CapabilityResult`, `CapabilityScope`, `DAG`, `DAGRun`, `DAGSpec`, `PendingReview`, `RiskLevel`, `RunState`, `RunTrace`, `ArtifactUpload` |
| Providers | `Provider`; `dagent.providers` also exports `ChatProvider`, `ChatResponse`, `ChatStreamEvent`, `MockProvider`, `OpenAICompatibleProvider`, and `ToolCall` for custom providers and tests |

Capability ids use the capability kind as their prefix. Python function tools use
`tool.*` ids. The old `custom_tool.*` kind has been removed instead of kept as a
compatibility alias.

## Profiles

Profiles are single Markdown files. A profile named `conversation` lives at
built-in package resource `dagent/resources/profiles/conversation.md`; the file
content is used as the system prompt. User profiles live in an explicit
`profile_root`. Profile references are names, not filesystem paths; pass the
directory as `profile_root` and use `ToolAgent(profile="reviewer")`.

```python
from dagent import AgentProfile, ProfileStore


profile = ProfileStore("profiles").load("conversation")
custom = AgentProfile(
    name="reviewer",
    description="Review assistant",
    content="# Reviewer\n\nReview code carefully.",
)
```

Use built-in profiles by name:

```python
agent = dagent.ToolAgent(profile="conversation")
```

Read packaged built-in profiles when you need to inspect or display them:

```python
profile = dagent.load_builtin_profile("conversation")
available = dagent.list_builtin_profiles()

print(profile.content)
print([item.name for item in available])
```

Use project profiles by passing a root to the runner:

```python
runner = dagent.Runner(provider=provider, profile_root="profiles")
agent = dagent.ToolAgent(profile="reviewer")
```

## Runner And Capabilities

`Runner` owns the capability catalog. Pass provider, capabilities, MCP servers,
skill roots, and profile roots explicitly for direct SDK integration. Config
files are a separate entrypoint: `Runner(...)` does not read `config.yaml`.

```python
provider = dagent.Provider(
    base_url="https://api.openai.com/v1",
    model="your-model",
    api_key_env="OPENAI_API_KEY",
)
runner = dagent.Runner(provider=provider)
```

For OpenAI-compatible endpoints with reasoning controls, `reasoning` provides a
small common shortcut. Use `extra_request_args` and `extra_body` only for
provider-specific parameters supported by the target endpoint:

```python
provider = dagent.Provider(
    base_url="https://api.deepseek.com",
    model="deepseek-v4-pro",
    api_key_env="DEEPSEEK_API_KEY",
    reasoning={"enabled": True, "effort": "high", "budget_tokens": 1024},
)
```

Use `Runner.from_config(...)` only when you want provider settings, configured
MCP servers, validation, or profile directories loaded from a config file:

```python
configured = dagent.Runner.from_config("config.yaml")
```

`config.yaml` can define provider settings, MCP servers, result validation, and
an optional user profile directory. Relative `profiles.directory` values resolve
from the config file directory. If `profiles.directory` is omitted, built-in
package profiles are used.

```yaml
provider:
  base_url: "https://api.deepseek.com"
  model: "deepseek-v4-pro"
  api_key_env: "DEEPSEEK_API_KEY"
  reasoning:
    enabled: true
    effort: "high"
    budget_tokens: 1024
```

Tools, MCP servers, and skill roots can be registered at construction.

```python
import dagent


@dagent.tool
def search(q: str) -> str:
    return f"found:{q}"


runner = dagent.Runner(
    workspace=".",
    provider=provider,
    capabilities=[search],
    mcp_servers={
        "fs": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        },
    },
    skill_roots=["team-skills"],
)
```

Capabilities can also be added later:

```python
runner = dagent.Runner(provider=provider, workspace=".")
runner.add_tool(search)
runner.add_skill_root("team-skills")

mcp_definitions = runner.add_mcp_server(
    "team_fs",
    {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
    },
)

print([definition.id for definition in mcp_definitions])

runner.replace_mcp_server(
    "team_fs",
    {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "docs"],
    },
)
runner.remove_mcp_server("team_fs")
```

`Runner` owns registered resources. Agents declare what they can use:
`capabilities` is the executable tool allowlist, while `skills` is the skill
allowlist exposed through the built-in `skill.list` and `skill.view` tools.
MCP tools become ordinary `mcp.<server>.<tool>` capability ids after server
registration.

MCP requires the optional extra (`pip install "dagent-ai[mcp]"`) and currently
supports stdio servers.

`Runner` also exposes capability management for hosts (such as the WebUI backend)
that build capabilities from raw definitions instead of `@dagent.tool` bindings:

```python
runner.register_capability(definition, handler, supports_context=False)
runner.replace_capability(definition, handler)
runner.set_capability_enabled("tool.search", False)
result = await runner.test_capability("tool.search", {"q": "dagent"})
runner.remove_capability("tool.search")

for definition in runner.list_capabilities(kind="mcp"):
    print(definition.id)

runner.enable_validation = True
trace = runner.run_trace(run_id)
```

## Tools And Structured Results

Decorate Python functions with `@dagent.tool`. Parameter annotations produce tool
input JSON schema; return annotations produce output schema.

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

Plain `str`, `dict`, `list`, numbers, booleans, tuples, bytes, and Pydantic
models are converted into `CapabilityResult.content` and `CapabilityResult.value`.
DAG node output references read from `value` by default.

If a tool returns `CapabilityResult` directly, completed results with no explicit
`value` use `content` as the value.

## AutoAgent

Use `AutoAgent` when the runtime should choose between a bounded tool loop and a
dynamic DAG for each request. `AutoAgent` has no mode field; use `ToolAgent` or
`DagAgent` when you want to force one path.

```python
import asyncio

import dagent


@dagent.tool
def search(q: str) -> str:
    return f"found:{q}"


async def main():
    runner = dagent.Runner(provider=provider, workspace=".", capabilities=[search])
    agent = dagent.AutoAgent(
        capabilities=["tool.search"],
        skills=["research/briefing"],
    )

    messages = [{"role": "user", "content": "Answer directly or plan if orchestration helps."}]
    result = await runner.run(agent, messages=messages)
    messages += result.messages
    print(result.kind)
    print(result.output_text)


asyncio.run(main())
```

## ToolAgent

Use `ToolAgent` for bounded tool-loop work where each next action depends on the
latest observation.

```python
import asyncio

import dagent


@dagent.tool
def echo(text: str) -> str:
    return f"echo:{text}"


async def main():
    runner = dagent.Runner(provider=provider, workspace=".", capabilities=[echo])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["tool.echo"],
        skills=["writing/terse"],
    )

    messages = [{"role": "user", "content": "Use echo to respond with hello."}]
    result = await runner.run(agent, messages=messages)
    if result.requires_review and result.review is not None:
        result = await runner.resume(result.review.approve())
    messages += result.messages

    print(result.output_text)


asyncio.run(main())
```

## DagAgent

Use `DagAgent` when the model should create a reviewable DAG, execute ready
layers, observe results, and replan when needed.

```python
import asyncio

import dagent


@dagent.tool
def search(q: str) -> str:
    return f"found:{q}"


async def main():
    runner = dagent.Runner(provider=provider, workspace=".", capabilities=[search])
    agent = dagent.DagAgent(
        capabilities=["tool.search"],
        skills=["research/briefing"],
        review="careful",
    )

    messages = [{"role": "user", "content": "Research X and write a concise report."}]
    result = await runner.run(agent, messages=messages)
    if result.requires_review and result.review is not None:
        result = await runner.resume(result.review.approve())
    messages += result.messages

    print(result.output_text)


asyncio.run(main())
```

## Static DAGs

Use `Dag` when the graph shape is known in code.

```python
import asyncio
from pathlib import Path

import dagent


@dagent.tool
def search(q: str) -> str:
    return f"found:{q}"


@dagent.tool(risk="medium", supports_context=True)
def write_note(path: str, content: str, *, context, callbacks=None) -> str:
    resolved = Path(context.workspace_path) / path
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"wrote:{path}"


async def main():
    dag = dagent.Dag("research_report", name="Research Report", input=str)
    report = dag.artifact("report", "outputs/report.md")

    search_node = dagent.Node("search", target=search, inputs={"q": dag.input})
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
    dag.add_node(search_node)
    dag.add_node(write_node)
    dag.add_edge(search_node, write_node)

    dagent.validate_dag_spec(dag.to_dag_spec())

    runner = dagent.Runner(provider=provider, workspace=".")
    result = await runner.run(dag, graph_input="dagent sdk")
    print(result.status)
    print(result.node_output("write_report"))


asyncio.run(main())
```

## DAG Node Parameter Passing

Static DAG arguments can contain value references. These references are serialized
as structured `$expr` bindings in the `DAGSpec` and resolved by `DAGExecutor`
immediately before the capability call.

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
| `artifact.path` | First declared artifact path |
| `artifact.paths` | All declared artifact paths |
| `artifact.absolute_path` | First artifact path resolved inside the run workspace |
| `artifact.absolute_paths` | All artifact paths resolved inside the run workspace |
| `dag.format("Use {x}", x=node.output)` | Format string after nested refs resolve |

Referencing a previous node output does not create an edge. Add the dependency
explicitly with `dag.add_edge(...)`:

```python
class SearchResult(BaseModel):
    title: str
    url: str


@dagent.tool
def search(q: str) -> SearchResult:
    return SearchResult(title=f"found:{q}", url="https://example.test")


@dagent.tool
def render(title: str, url: str) -> str:
    return f"{title} <{url}>"


dag = dagent.Dag("research", input=dict)
search_node = dagent.Node("search", target=search, inputs={"q": dag.input.query})
render_node = dagent.Node(
    "render",
    target=render,
    inputs={
        "title": search_node.output.title,
        "url": search_node.output.url,
    },
)
dag.add_node(search_node)
dag.add_node(render_node)
dag.add_edge(search_node, render_node)
```

Pydantic graph inputs are accepted at runtime and converted to JSON-like data
before path lookup:

```python
from pydantic import BaseModel


class QueryInput(BaseModel):
    query: str


dag = dagent.Dag("research", input=QueryInput)
search_node = dagent.Node("search", target=search, inputs={"q": dag.input.query})
dag.add_node(search_node)

await runner.run(dag, graph_input=QueryInput(query="dagent"))
```

Artifact references can be used in arguments and boundaries:

```python
report = dag.artifact("report", "outputs/report.md")

write_node = dagent.Node(
    "write_report",
    target=write_note,
    inputs={"path": report.path},
    boundary=dagent.Boundary(mode="write_limited", allowed_paths=[report.path.as_expr()]),
    artifact_outputs=[report],
)
dag.add_node(write_node)
```

Validation fails closed when a node reads from a non-upstream node, references an
unknown artifact, or uses a malformed value expression.

## Results And Streaming

`Runner.run(...)` returns `RunResult` for every public target: `AutoAgent`,
`ToolAgent`, `DagAgent`, `Dag`, and `DAGSpec`.

```python
messages = [{"role": "user", "content": "Write the report."}]
result = await runner.run(agent, messages=messages)

print(result.kind)         # "tool", "dynamic_dag", or "static_dag"
print(result.status)
print(result.output_text)
print(result.trace)
```

For agent targets, `result.messages` contains only the messages generated by the
current run. Append them to your caller-maintained conversation. `result.state`
contains dagent's resumable internal thread, DAG, trace, pending review, and
static DAG metadata. `RunResult.output_text` is the canonical final answer;
`state.internal_messages` is the provider conversation needed for continuation,
and `state.trace` is the audit timeline rather than another full-text answer
mirror.

```python
messages += result.messages
saved_state = result.state

messages.append({"role": "user", "content": "Continue with one more detail."})
result = await runner.run(agent, messages=messages, state=saved_state)
```

If you persist the full result payload, restore the current SDK shape with
`RunResult.model_validate(...)` and pass the restored state back to the matching
entry point. Use `run(..., state=...)` for normal continuation. If the saved
state is awaiting review, continue that checkpoint with `resume(..., state=...)`;
`run(..., state=...)` rejects awaiting-review states so review gates cannot be
accidentally bypassed.

```python
saved_payload = result.model_dump(mode="json")
restored = dagent.RunResult.model_validate(saved_payload)

if restored.requires_review and restored.review is not None:
    result = await runner.resume(restored.review.approve(), state=restored.state)
else:
    messages.append({"role": "user", "content": "Continue."})
    result = await runner.run(agent, messages=messages, state=restored.state)
```

For static DAGs, DAG-oriented helpers are available on the same result object:

```python
result = await runner.run(dag, graph_input="dagent", workspace_root="runs")

print(result.workspace_path)
print(result.node_output("write_report"))
print(result.node_value("search"))
print(result.artifact_state("report").status)
```

`DAGRun` remains a schema for API projections and is available through
`result.dag_run` for static DAG runs; it is not dumped as a top-level
`RunResult` field.

`Runner.stream(...)` is the single streaming entry point. It runs the target and
yields typed `RunStreamEvent` objects with a uniform envelope: `type`, `data`,
`sequence`, and `run_id`. Once execution starts, the stream opens with
`run.started` — its envelope `run_id` is the final run id, so consumers never
wait for the stream tail to correlate events — and always settles with exactly
one `run.finished` or `run.failed`. Pre-run request validation errors can settle
directly as `run.failed` before a run id exists.

To consume only the generated text, filter on `response.content.delta`:

```python
async for event in runner.stream(agent, messages=messages):
    if event.type == "response.content.delta":
        print(event.data.delta, end="")
    elif event.type == "trace.updated":
        print(event.data.trace.status)
    elif event.type == "review.required":
        print(event.data.message)
    elif event.type == "run.finished":
        print(event.data.result.output_text)
```

The full event protocol:

| Event type | Primary fields |
|------------|----------------|
| `run.started` | `event.data.kind`; envelope `run_id` is the final run id |
| `response.started` | response identity fields (see below) |
| `response.reasoning.delta` | `event.data.delta`, structured provider reasoning or text inside `<think>...</think>` |
| `response.content.delta` | `event.data.delta`, assistant answer text outside reasoning |
| `response.finished` | response identity fields |
| `capability.call.started` | `event.data.invocation_id`, `event.data.capability_id`, `event.data.arguments`, optional `run_id` and DAG context fields |
| `capability.call.completed` / `capability.call.failed` | `event.data.invocation_id`, `event.data.capability_id`, `event.data.content`, optional `run_id` and DAG context fields |
| `dag.updated` | `event.data.dag`, emitted only when the DAG changed |
| `trace.updated` | `event.data.trace`, emitted only when the trace changed |
| `validation.started` / `validation.passed` / `validation.retry` | `event.data` |
| `review.required` | `event.data.review_id`, `event.data.kind`, `event.data.message` |
| `run.finished` | `event.data.result` |
| `run.failed` | `event.data.message`, `event.data.error_type` |

Every streamed text source is bracketed by `response.started` and
`response.finished`. This includes model calls and capabilities that explicitly
emit token callbacks. Each source uses an isolated token splitter, so parallel
DAG nodes never bleed tokens into each other. All `response.*` events carry the
same identity fields: `response_id` (the stable per-source key), `model_step`,
and — when the source runs inside a DAG node — `run_id`, `dag_id`, `node_id`,
and `parent_capability_id`.
Group deltas by `response_id`, not by ordering or `model_step`: under parallel
nodes, retries, and resume, only `response_id` is unique. Whitespace between
`</think>` and the answer is dropped from the content channel, so concatenated
`response.content.delta` text matches `RunResult.output_text`.

`review.required` is a lightweight signal carrying only `review_id`, `kind`, and
`message`. The contract: the `run.finished` event that follows carries the full
pending review in `event.data.result.state.pending_review`, including
`capability_call`, any `proposed_dag`, and the review `payload` — build review
UIs from that, not from the signal event.

`RunStreamEvent.model_dump(mode="json")` returns a JSON-ready event payload. If
the event has a result, the nested value is `RunResult.model_dump(mode="json")`.
The `run.finished` payload carries the complete `state`; restore it with
`RunResult.model_validate(...)` if you need to continue from saved JSON.

Use `Runner.resume_stream(...)` to continue a pending review with the same
event contract; its `run.started` carries the resumed run's id:

```python
first = await runner.run(agent, messages=[{"role": "user", "content": "Write the report."}])

if first.requires_review and first.review is not None:
    async for event in runner.resume_stream(first.review.approve()):
        if event.type == "response.content.delta":
            print(event.data.delta, end="")
        elif event.type == "run.finished":
            print(event.data.result.output_text)
```

For a pending review restored after a restart, pass the saved state to
`resume_stream(...)`:

```python
restored = dagent.RunResult.model_validate(saved_payload)

if restored.requires_review and restored.review is not None:
    async for event in runner.resume_stream(restored.review.approve(), state=restored.state):
        ...
```

## Skills

`Runner` exposes the skill store used by the built-in skill accessors. Concrete
skills are not capabilities; agents list readable skills with the `skills`
field.

```python
runner = dagent.Runner(provider=provider, workspace=".")
runner.add_skill_root("team-skills")

installed = runner.skill_store.install(
    "Keep every answer to one compact sentence.",
    name="terse",
    description="Compact response style.",
    category="writing",
)

print(installed.skill.qualified_name)
print(runner.skill_store.view("writing/terse").content)
print(runner.skill_store.view("writing/terse", file_path="scripts/example.py").content)

agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["tool.read_file"],
    skills=["writing/terse"],
)
```

`SkillStore.install(...)` writes Markdown or zip skill packages into the managed
root. `view(name, file_path=...)` reads linked files with path traversal checks.
Use `skills=None` to allow all configured skills, `skills=[]` to hide skill
tools, and `skills=[...]` to expose only the named skills.
