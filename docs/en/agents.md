# Agents

dagent has three public agent configurations: `ToolAgent`, `AutoAgent`, and
`DagAgent`. They are declarative run targets. `Runner` owns provider clients,
capabilities, runtime state, review continuation, and execution dispatch.

## Choosing an Agent

| Agent | Use when |
| --- | --- |
| `ToolAgent` | The model should use a bounded tool loop and choose each next action from the latest observation. |
| `AutoAgent` | The runtime should choose direct tool use or dynamic DAG planning per request. |
| `DagAgent` | The model should plan a reviewable DAG, execute ready layers, observe results, and replan locally. |

Use static `Dag` instead of an agent when the graph shape belongs in code. See
[Static DAGs](static-dag.md).

## Managed Profiles and Agent Presets

Built-in profiles live in `dagent/resources/profiles/*.md`. The local FastAPI
service manages editable profiles under `~/.dagent/profiles/<name>.md`; users can
create, copy, edit, and delete those profiles without passing Markdown file
paths to each run.

Managed profile names are product identifiers used by agent capabilities, so
they must start with a letter and may contain only letters, numbers, and `_`.
A managed profile named `analyst` is exposed to the static DAG editor as
`agent.analyst`.

The local API also stores reusable agent presets under
`~/.dagent/agents/*.json`. An agent preset chooses a profile plus the tools, MCP
capabilities, and skills that the child agent may use. Chat and dynamic DAG runs
can expose those presets with `agent_scope="selected"` and
`agent_ids=["agent.<name>"]`, or with `agent_scope="registered"` for all
registered presets.

Preset JSON uses the `ToolAgent` field names: `name`, `profile`,
`capabilities`, `skills`, `agents`, `review`, `max_steps`, and `description`.
Registered presets are leaf subagents, so `agents` must be empty and `review`
must be `"fast"`. The local API validates the preset before writing it to the
workspace; old fields such as `capability_ids` are rejected rather than
converted.

## ToolAgent

```python
import asyncio

import dagent


@dagent.tool
def echo(text: str) -> str:
    return f"echo:{text}"


async def main():
    runner = dagent.Runner(provider=provider, workspace=".dagent", capabilities=[echo])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["tool.echo"],
        skills=["writing/terse"],
        max_steps=8,
        review="fast",
    )

    result = await runner.run(
        agent,
        messages=[{"role": "user", "content": "Use echo to respond with hello."}],
    )
    print(result.output_text)
    runner.close()


asyncio.run(main())
```

Run the offline example:

```bash
uv run python -m examples.tool_agent
```

### Tool Selection

The built-in `conversation` profile, titled **General-Purpose Agent**, prefers
an available tool when it directly fulfills the user's request, including simple
one-tool tasks. It still answers greetings and requests without a clearly
relevant tool directly, and does not call tools merely because they are enabled.
This is model-directed behavior, not a forced invocation guarantee; use a
dedicated custom profile when a workflow requires a particular tool.

## AutoAgent

`AutoAgent` has no mode field. It routes each request to direct tool use or
dynamic DAG planning.

```python
agent = dagent.AutoAgent(
    profile="conversation",
    planner_profile="dag_agent",
    capabilities=["tool.search"],
    skills=["research/briefing"],
    max_steps=8,
    max_cycles=6,
    review="fast",
    dynamic_adjust=True,
)

messages = [{"role": "user", "content": "Answer directly or plan if orchestration helps."}]
result = await runner.run(agent, messages=messages)
messages += result.messages
```

Run the offline example:

```bash
uv run python -m examples.auto_agent
```

## DagAgent

`DagAgent` is for dynamic DAG planning. It can pause for human review before
executing proposed work. Planner calls use a strict JSON Schema response: the
model chooses `propose_plan`, `no_change`, or `final_answer`. The Runner-wide
planner frontend determines whether a proposal contains a typed plan or
restricted Builder source; both normalize to canonical `DAGSpec` before
validation, review, and execution. Capability nodes reference stable ids such
as `tool.search`; the host supplies kind, risk, boundaries, defaults, and
invocation identity.

Typed dynamic plans can contain capability/agent, map, subgraph, and bounded
loop nodes, explicit conditional edges, artifacts, graph output, and structured
value references. They use the same validator and executor as static DAGs.

`typed_spec` remains the default. To opt into code-oriented authoring, construct
the runner with `planner_frontend="sdk_builder"`. The generated source uses a
straight-line allowlisted Builder subset and is never passed to `exec` or
`eval`. It may express the same maps, subgraphs, bounded loops, artifacts,
references, outputs, and conditional edges; canonical `DAGSpec`, not source,
is the review and persistence object.

```python
agent = dagent.DagAgent(
    planner_profile="dag_agent",
    capabilities=["tool.search"],
    skills=["research/briefing"],
    max_cycles=6,
    review="careful",
    dynamic_adjust=True,
)

result = await runner.run(
    agent,
    messages=[{"role": "user", "content": "Research dagent and write a note."}],
)

if result.requires_review and result.review is not None:
    result = await runner.resume(result.review.approve())
```

Set `dynamic_adjust=False` when you want the planner to generate the initial DAG
but keep that DAG fixed during execution. Review behavior is still controlled by
`review`; disabling dynamic adjustment only prevents later replanning after
execution observations or failures.

Custom providers used with `DagAgent` must implement the `response_format`
keyword on `chat(...)` and `stream_chat(...)` and honor strict JSON Schema
structured output. The built-in `Provider` maps this contract to OpenAI-compatible
Chat Completions `json_schema` response formatting.

Run the offline dynamic DAG example:

```bash
uv run python -m examples.dynamic_dag_agent
uv run python -m examples.dynamic_dag_builder_agent
```

## Subagent Delegation

Top-level `ToolAgent`, `AutoAgent`, and `DagAgent` runs can expose registered
`ToolAgent` subagents as `agent.*` capabilities. Subagents are leaf agents:
they can use their configured tools, MCP capabilities, and skills, but they
cannot call another subagent. Registered subagents must use `review="fast"`;
the top-level agent owns review behavior for the delegated call.

```python
helper = dagent.ToolAgent(
    profile="conversation",
    name="helper",
    capabilities=["tool.search"],
    skills=["research/briefing"],
    max_steps=4,
    description="Research helper.",
)

runner.add_agent(helper)

agent = dagent.DagAgent(
    capabilities=["tool.read_file"],
    agents=["agent.helper"],
)
```

You can also pass a `ToolAgent` object directly in `agents=[helper]`; the runner
registers it before the run. Use `agents="registered"` to expose every agent
registered on that runner. Passing `capabilities=None` still excludes `agent.*`
capabilities by default; use `agents=...` or explicitly include an `agent.*`
capability id when the top-level run should delegate.

Dynamic DAG planners see exposed agents in the Available Tools section and call
them like any other function, usually with `prompt="..."` and optionally
`reference_content="..."` or `max_steps=...`. Non-empty reference content is
supplied to the agent as task data in a separate user-message section.

## Shared Agent Fields

| Field | Meaning |
| --- | --- |
| `profile` | Tool-loop system prompt, either a built-in name, user profile name, or `AgentProfile`. |
| `planner_profile` | Dynamic DAG planner profile for `AutoAgent` and `DagAgent`. |
| `capabilities` | Capability ids or `@dagent.tool` bindings visible to the agent. |
| `skills` | Concrete skills visible through `skill.list` and `skill.view`. |
| `agents` | Subagent capabilities visible to a top-level run: `None`, `"registered"`, `ToolAgent` objects, or `agent.<name>` ids. |
| `review` | Review level for risky work. |
| `max_steps` | Tool-loop bound for `ToolAgent` and `AutoAgent`. |
| `max_cycles` | Dynamic DAG replan bound for `AutoAgent` and `DagAgent`. |
| `dynamic_adjust` | Whether `AutoAgent` and `DagAgent` may replan the dynamic DAG after the initial DAG is generated. Defaults to `True`. |

Passing `capabilities=None` uses the runner's default visible capabilities.
Passing an explicit list narrows the agent to that set.

## Conversation Continuation

Agent runs accept OpenAI-compatible `messages`. The result only contains
messages generated by the current run, so append them before continuing:

```python
messages += result.messages
messages.append({"role": "user", "content": "Continue with one more detail."})
result = await runner.run(agent, messages=messages, state=result.state)
```

See [Results, Streaming, and Review](results-streaming-review.md) for persistence
and review-safe resume flows.
