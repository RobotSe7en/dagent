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

Runner-level `extra_system_prompt` is part of the resolved run plan rather than
the conversation. A review resume restores the value frozen in the checkpoint.

Do not append `result.new_items` yourself. They are the audit delta for the run;
`result.conversation` is already the complete bounded state to pass next time.

## What reaches the model

Before every model call, one context assembler creates the OpenAI-compatible
request in this order:

1. the current system prompt;
2. an earlier-conversation summary, when present;
3. recent typed conversation/model-thread items;
4. tool schemas or planner response schemas.

Assistant reasoning remains available on `AssistantMessage.reasoning` for
display and audit. `ContextPolicy.reasoning_replay` controls its request
projection: the default `active_run` replays reasoning only between model/tool
steps of the current run, `none` never replays it, and `all_runs` also replays
earlier conversation runs.

Tool calls and matching tool results remain structurally complete. Tool-result
text can be head/tail truncated for model context without deleting its audit
record or breaking `tool_call_id` pairing.

## Context limits and compaction

The private-vLLM provider uses `/tokenize` for exact request counts and
`max_model_len` when available. Automatic discovery is the default; an explicit
context value overrides it but cannot exceed a discovered server limit. Failed
discovery warns and falls back to a 32K context window. Output length is unset
by default:

```python
provider = dagent.Provider(
    base_url="http://localhost:8000/v1",
    model="local-model",
    context_window_tokens=None,
    max_output_tokens=None,
)
```

Configure per-agent context behavior with `ContextPolicy`:

```python
agent = dagent.ToolAgent(
    profile="conversation",
    context=dagent.ContextPolicy(
        reasoning_replay="active_run",
        compaction_trigger_ratio=0.8,
        compaction_retain_ratio=0.16,
        summary_max_tokens=8192,
        compaction_reasoning_effort="low",
        max_tool_result_tokens=2048,
        max_total_tool_result_tokens=8192,
    ),
)
```

When the trigger is crossed, dagent summarizes complete older runs, omits the
oldest replayed reasoning from the active request if needed, then summarizes
completed middle steps of an oversized active run. This is token-driven; there
is no minimum retained-turn count. The current user input, open tool chain, and
latest atomic step remain. The normal compaction path uses the configured model
and consumes one model turn from telemetry. If that call fails, a deterministic
bounded summary is used and the fallback reason is recorded. If mandatory input
still does not fit, `ContextWindowExceeded` is raised before generation.
The compactor request has its own output limit and reasoning effort.
`ContextSummary` records whether its source was truncated, provider usage,
model-call metadata, and context estimate. Summary reasoning is discarded; only
`ContextSummary.content` is projected later.

Inspect `result.context_usage` for exact/estimated counts, the discovered
server limit, reasoning replay/omission, included/compacted item counts,
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
capability and skill scope, capability-definition fingerprints, policies,
context-window and output-reserve limits, planner mode, and prior execution
usage so review cannot resume under different semantics. If a resumed run
reaches another review gate, its replacement checkpoint keeps those same frozen
limits even if provider settings changed meanwhile.

The same checkpoint flow applies to supported static DAG agent-node reviews.
The checkpoint includes the suspended node invocation and its internal
tool-agent state; its resolved capability scope includes the registered agent's
inner tools. The direct agent-node execution configuration is fingerprinted, so
a changed profile or runtime setting cannot silently alter a resumed run. See
[Static DAGs](static-dag.md#agent-node-tool-review) for the supported topology
and policy behavior.

## Steer an active tool-agent run

Use `Runner.steer(...)` to add text guidance to a currently executing root
`ToolAgent` run without cancelling its in-flight model or capability call. Give
the run an explicit id, start it in a task, then wait until your application
knows the run has started before steering it:

```python
run_task = asyncio.create_task(
    runner.run(
        agent,
        input="Draft the release note.",
        run_id="release_note_run",
    )
)

# Called later, while the run is active.
receipt = await runner.steer(
    "release_note_run",
    "Focus on the breaking API change and omit benchmarks.",
)
assert receipt.status == "queued"

result = await run_task
```

`steer` acknowledges queueing immediately. The tool loop applies queued
messages FIFO as separate `UserMessage` items at the next cooperative safe
point: before a model call, after the current model call, or after the current
capability call. It never interrupts a model request or a capability that has
already started. If one assistant response requested multiple capabilities, the
current call finishes and calls that have not started are skipped so the model
can reconsider them against the new guidance.

The mailbox holds at most 32 messages. Rejections are explicit:

- `RunNotActiveError`: the run has not started or has already ended;
- `RunNotSteerableError`: the active run is a DAG, is validating, or is waiting
  for review;
- `SteerQueueFullError`: 32 messages are already queued.

Only root tool-agent execution is steerable. `AutoAgent` becomes steerable only
after routing resolves to `tool`; a `dynamic_dag` route is rejected. Static and
dynamic DAG execution are never steerable. A nested subagent does not consume
the root mailbox—the root loop applies the guidance after the subagent returns.

Steering is also available while `resume(...)` or `resume_stream(...)` is
actively continuing an approved tool-agent review. A run already stopped at
`awaiting_review` rejects `steer`; put guidance in the review decision's
`feedback` instead. Applied steering is included in the request passed to result
validation, while `RunState.user_request` remains the original request.

Steering does not extend `ToolAgent.max_steps`. If no model step remains, queued
guidance is discarded and the run fails with reason `step_limit_exhausted`.
Other discard reasons are `run_cancelled`, `run_failed`, and `runner_closed`.
The runnable example is [`examples/steering.py`](../../examples/steering.py).

## Streaming

```python
async for event in runner.stream(agent, input="Prepare the answer."):
    if event.type == "response.reasoning.delta":
        show_reasoning(event.data.delta)
    elif event.type == "response.content.delta":
        show_content(event.data.delta)
    elif event.type == "context.compaction.finished":
        show_context_usage(event.data.usage)
    elif event.type == "steer.queued":
        show_queued_steer(event.data.steer_id, event.data.content)
    elif event.type == "steer.applied":
        show_applied_steer(event.data.steer_id)
    elif event.type == "steer.discarded":
        show_discarded_steer(event.data.steer_id, event.data.reason)
    elif event.type == "run.finished":
        result = event.data.result
```

Review continuation has the parallel `resume_stream(decision,
checkpoint=checkpoint)` API. `run.finished` contains the same `RunResult` shape
as non-streaming execution. Its serialized result includes `output_value`; for
static runs this is the exact resolved `DAGSpec.output`, while `output_text`
keeps the compatibility rendering. `RunStreamEvent.model_validate(...)` restores
the same typed event payload and uses the envelope `type` to preserve the exact
data class even when multiple event payloads have identical fields.
