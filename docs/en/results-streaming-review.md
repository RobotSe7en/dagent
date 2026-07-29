# Conversations, results, streaming, and review

dagent 0.8 separates cross-run conversation continuation from same-run review
continuation. This is an intentional breaking change:

- `ConversationState` continues a conversation across independent runs.
- `RunCheckpoint` resumes a run that stopped at a review gate.
- raw OpenAI `messages` and `RunState` are not continuation inputs.

## Continue a conversation

Each call supplies only the new user turn. Pass the bounded conversation returned
by the previous result:

```python
first = await runner.run(agent, input="Remember that the release color is blue.")

second = await runner.run(
    agent,
    input="What is the release color?",
    conversation=first.conversation,
)
print(second.output_text)
```

`ConversationState` is provider-neutral. It contains typed user, assistant, and
tool-result items, plus an optional summary and a revision. It never contains a
system prompt or provider request options.

Do not append `result.new_items` yourself. They are the audit delta for the run;
`result.conversation` is already the complete bounded state to pass next time.

## What reaches the model

Before every model call, one context assembler creates the OpenAI-compatible
request in this order:

1. the current system prompt;
2. an earlier-conversation summary, when present;
3. recent typed conversation/model-thread items;
4. tool schemas or planner response schemas.

Assistant reasoning is deliberately excluded from this projection. It remains
available on `AssistantMessage.reasoning` for display and audit, but it is never
replayed to a later model call.

Tool calls and matching tool results remain structurally complete. Tool-result
text can be head/tail truncated for model context without deleting its audit
record or breaking `tool_call_id` pairing.

## Context limits and compaction

OpenAI-compatible local endpoints do not reliably publish their context size, so
the provider defaults to a 32K context window and a 4K output reserve:

```python
provider = dagent.Provider(
    base_url="http://localhost:8000/v1",
    model="local-model",
    context_window_tokens=32768,
    output_reserve_tokens=4096,
)
```

Configure per-agent context behavior with `ContextPolicy`:

```python
agent = dagent.ToolAgent(
    profile="conversation",
    context=dagent.ContextPolicy(
        compaction_trigger_ratio=0.8,
        keep_recent_turns=4,
        summary_max_tokens=1024,
        max_tool_result_tokens=2048,
        max_total_tool_result_tokens=8192,
    ),
)
```

When the trigger is crossed, dagent summarizes complete old turns and retains
recent turns. The normal path uses the configured model and consumes one model
turn from the execution budget. If that call fails, a deterministic bounded
summary is used and the fallback reason is recorded. If mandatory input still
does not fit, `ContextWindowExceeded` is raised before the provider is called.
The compactor request is independently budgeted. `ContextSummary` records
whether its source was truncated, its provider usage, context estimate, and any
captured reasoning; only `ContextSummary.content` is projected later.

Inspect `result.context_usage` for estimates, included/compacted item counts,
tool-result truncation, and the compaction method.

## Reasoning and provider usage

OpenAI-compatible `reasoning_content`/`reasoning` fields and `<think>...</think>`
content are normalized into `AssistantMessage.reasoning`. Visible content is
kept separately:

```python
for item in result.new_items:
    if isinstance(item, dagent.AssistantMessage):
        print(item.content)
        print(item.reasoning)
        print(item.usage)  # provider-reported usage, when supplied
```

Reasoning deltas use `response.reasoning.delta` in the typed stream protocol.
Content deltas use `response.content.delta`.

## Large tool and MCP results

Tool and MCP text up to 256 KiB stays inline by default. Larger text, binary
values, and MCP binary payloads are written atomically under the run workspace
and represented by checksum-bearing `ContentReference` values. The model sees a
bounded preview plus the workspace-relative reference.

```python
runner = dagent.Runner(
    workspace="agent-workspace",
    runtime_directory=".runtime",
    provider=provider,
    result_storage_policy=dagent.ResultStoragePolicy(
        max_inline_bytes=256 * 1024,
    ),
)
```

The result directory is `<run-workspace>/.runtime/results` for this runner.
`ResultStoragePolicy` controls only the inline-size threshold; the runner owns
the location.

The SDK owns only run-workspace normalization. A host is responsible for durable
upload, retention, access control, and URL generation.

Static DAG traces retain typed references for externalized values and
`stdout`/`stderr`/error fields. Map-node parent values remain bounded; the
executor resolves their indexed references only when an authorized downstream
value expression reads them. This keeps checkpoints JSON-safe while preserving
full dataflow and audit recovery.

## Resume a review

Persist the full checkpoint whenever a run awaits review:

```python
result = await runner.run(agent, input="Write the release note.")

if result.requires_review:
    checkpoint_json = result.checkpoint.model_dump_json()
```

Restore it and resume through the dedicated API:

```python
checkpoint = dagent.RunCheckpoint.model_validate_json(checkpoint_json)
decision = result.review.approve(feedback="Proceed and keep it concise.")

resumed = await runner.resume(
    decision,
    checkpoint=checkpoint,
)
```

`Runner.run(..., checkpoint=...)`, `run(..., state=...)`, and
`resume(..., state=...)` do not exist in 0.8. A checkpoint freezes profiles,
host prompt extensions, capability and skill scope, capability-definition
fingerprints, policies, context-window and output-reserve limits, planner mode,
and prior execution usage so review cannot resume under different semantics. If
a resumed run reaches another review gate, its replacement checkpoint keeps
those same frozen settings even if the current runner or provider configuration
changed meanwhile.

Runs with prompt extensions use checkpoint schema V5 because the resolved plan
includes the canonical extension snapshot in its fingerprint. Default
no-extension runs continue to produce V4, and existing V4 checkpoints remain an
explicit no-extension format that resumes without conversion.
`ConversationState` remains V3; passing it to `Runner.run(...)` starts a new run
under that runner's current configuration.

## Streaming

```python
async for event in runner.stream(agent, input="Prepare the answer."):
    if event.type == "response.reasoning.delta":
        show_reasoning(event.data.delta)
    elif event.type == "response.content.delta":
        show_content(event.data.delta)
    elif event.type == "context.compaction.finished":
        show_context_usage(event.data.usage)
    elif event.type == "run.finished":
        result = event.data.result
```

Review continuation has the parallel `resume_stream(decision,
checkpoint=checkpoint)` API. `run.finished` contains the same `RunResult` shape
as non-streaming execution.
