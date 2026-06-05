# Python SDK Guide

This guide covers the current public Python SDK. Runnable examples live in
[`examples/`](../examples/).

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
| Reviews and results | `RunResult`, `RunStreamChunk`, `RunStreamEvent`, `ReviewHandle`, `ReviewDecision`, `ReviewLevel` |
| Runtime schemas | `Boundary`, `CapabilityDefinition`, `CapabilityInvocation`, `CapabilityPolicy`, `CapabilityResult`, `CapabilityScope`, `DAG`, `DAGRun`, `DAGSpec`, `PendingReview`, `RiskLevel`, `RunTrace`, `RuntimeResponse`, `ArtifactUpload` |
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

Use `Runner.from_config(...)` only when you want provider settings, configured
MCP servers, validation, or profile directories loaded from a config file:

```python
configured = dagent.Runner.from_config("config.yaml")
```

`config.yaml` can define provider settings, MCP servers, result validation, and
an optional user profile directory. Relative `profiles.directory` values resolve
from the config file directory. If `profiles.directory` is omitted, built-in
package profiles are used.

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

MCP requires the optional extra (`pip install "dagent[mcp]"`) and currently
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
trace = runner.task_trace(run_id)
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

    result = await runner.run(agent, "Answer directly or plan if orchestration helps.")
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

    result = await runner.run(agent, "Use echo to respond with hello.")
    if result.requires_review and result.review is not None:
        result = await runner.resume(result.review.approve())

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

    result = await runner.run(agent, "Research X and write a concise report.")
    if result.requires_review and result.review is not None:
        result = await runner.resume(result.review.approve())

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
    result = await runner.run(dag, input="dagent sdk")
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
| `dag.input` | Whole `Runner.run(dag, input=...)` value |
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

await runner.run(dag, input=QueryInput(query="dagent"))
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
result = await runner.run(agent_or_dag, input)

print(result.kind)         # "tool", "dynamic_dag", or "static_dag"
print(result.status)
print(result.output_text)
print(result.trace)
```

For static DAGs, `RunResult` exposes the underlying `DAGRun` as `dag_run` and
keeps DAG-oriented helpers on the same result object:

```python
result = await runner.run(dag, input="dagent", workspace_root="runs")

print(result.workspace_path)
print(result.node_output("write_report"))
print(result.node_value("search"))
print(result.artifact_state("report").status)
```

`DAGRun` remains a schema for API/storage boundaries and is available through
`result.dag_run` or `result.raw_response`; it is not the primary return value
from the public runner.

Use `Runner.stream(...)` for an async stream of `RunStreamChunk` objects. Chunks
surface the common values directly: generated text, pending reviews, and the
final unified `RunResult`.

```python
async for chunk in runner.stream(agent_or_dag, input):
    if chunk.text:
        print(chunk.text, end="")
    if chunk.review:
        print(chunk.review.message)
    if chunk.result:
        print(chunk.result.output_text)
        print(chunk.result.model_dump(mode="json"))
```

Each chunk also carries the underlying `chunk.event` for callers that want the
full event envelope.

Use `Runner.stream_events(...)` when you want to forward, persist, or inspect the
complete low-level event stream. Events use a uniform envelope:
`type`, `data`, `sequence`, and `run_id`.

```python
async for event in runner.stream_events(agent_or_dag, input):
    if event.type == "response.output_text.delta":
        print(event.data.delta, end="")
    elif event.type == "trace.updated":
        print(event.data.trace.status)
    elif event.type == "review.required":
        print(event.data.message)
    elif event.type == "run.finished":
        print(event.data.result.output_text)
```

Common stream event payloads:

| Event type | Primary fields |
|------------|----------------|
| `response.output_text.delta` | `event.data.delta` |
| `run.status` | `event.data.message` |
| `capability.call.started` | `event.data.invocation_id`, `event.data.capability_id`, `event.data.arguments`, optional DAG context fields |
| `capability.call.completed` / `capability.call.failed` | `event.data.invocation_id`, `event.data.capability_id`, `event.data.content`, optional DAG context fields |
| `dag.updated` | `event.data.dag` |
| `trace.updated` | `event.data.trace` |
| `review.required` | `event.data.message`, `event.data.to_handle()` |
| `validation.started` / `validation.passed` / `validation.retry` | `event.data` |
| `run.finished` | `event.data.result` |
| `run.failed` | `event.data.message`, `event.data.error_type` |

`RunStreamEvent.model_dump(mode="json")` returns a JSON-ready event payload. If
the event has a result, the nested value is `RunResult.model_dump(mode="json")`.

Use `Runner.resume_stream(...)` to continue a pending review with the same
event contract:

```python
first = await runner.run(agent, "Write the report.")

if first.requires_review and first.review is not None:
    async for chunk in runner.resume_stream(first.review.approve()):
        if chunk.text:
            print(chunk.text, end="")
        if chunk.result:
            print(chunk.result.output_text)
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
