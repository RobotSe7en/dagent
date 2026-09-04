# Model Context and Reasoning

dagent uses one provider-neutral conversation model for private vLLM models and
serializes each request to either OpenAI Chat Completions or Responses. The
runtime does not persist provider response IDs or depend on server-side state.

## One run and multiple runs

A **run** starts with one user input and may contain multiple model/tool steps:

```text
user -> reasoning + tool call -> tool result -> reasoning + tool call -> ... -> answer
```

A later user input is a new run, even when it continues the same
`ConversationState`. The default policy is:

```python
agent = dagent.ToolAgent(
    profile="conversation",
    context=dagent.ContextPolicy(reasoning_replay="active_run"),
)
```

The available modes are:

- `none`: never put stored reasoning back into model input;
- `active_run`: replay reasoning produced earlier in the current run, so the
  model can continue after a tool result without re-deriving its plan;
- `all_runs`: also replay reasoning from earlier user runs in the continued
  conversation.

Reasoning is always retained in `AssistantMessage.reasoning` for audit. Replay
policy only changes the next request projection. It does not delete audit data.

## The same logical request on both protocols

Assume the current run contains a user request, an assistant reasoning trace and
tool call, then a tool result. In Chat Completions, a detected vLLM server sees:

```json
[
  {"role": "system", "content": "..."},
  {"role": "user", "content": "Find the release."},
  {
    "role": "assistant",
    "content": "",
    "reasoning": "I should inspect the repository.",
    "tool_calls": [{
      "id": "call_1",
      "type": "function",
      "function": {"name": "read_file", "arguments": "{\"path\":\"CHANGELOG.md\"}"}
    }]
  },
  {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "..."}
]
```

`chat_reasoning_field="reasoning_content"` changes only the assistant replay
key. `"omit"` removes it. `"auto"` chooses `reasoning` for vLLM and `omit` for
an unknown server.

The equivalent stateless Responses input is:

```json
[
  {"role": "user", "content": "Find the release."},
  {
    "type": "reasoning",
    "id": "rs_<stable-local-digest>",
    "summary": [],
    "content": [{"type": "reasoning_text", "text": "I should inspect the repository."}]
  },
  {"type": "function_call", "id": "fc_<stable-local-digest>", "call_id": "call_1", "name": "read_file", "arguments": "{\"path\":\"CHANGELOG.md\"}"},
  {"type": "function_call_output", "call_id": "call_1", "output": "..."}
]
```

The request also sends `instructions`, flattened Responses function tools,
`store=False`, and the selected structured-output format. IDs needed by the
wire shape are deterministically derived from local conversation item IDs; they
are not vLLM response IDs. dagent never sends `previous_response_id` or
encrypted content.

When the user sends the next message, `active_run` still includes earlier
assistant content and tool observations but omits their reasoning. `all_runs`
keeps the reasoning items too.

## Reasoning controls

```python
provider = dagent.Provider(
    base_url="http://localhost:8000/v1",
    model="Qwen/Qwen3-Coder",
    protocol="auto",
    reasoning_effort="medium",
    reasoning_capture="field_and_tags",
    context_window_tokens=None,
    max_output_tokens=None,
)
```

`reasoning_effort` accepts `none`, `minimal`, `low`, `medium`, `high`, `xhigh`,
or `max`. The SDK sends it as `reasoning_effort` to Chat or `reasoning.effort`
to Responses. Model support is still determined by the model served by vLLM.
Token-based reasoning budgets are not part of the SDK contract.

`reasoning_capture="field_and_tags"` combines the dedicated response reasoning
field with `<think>` content. `reasoning_capture="field"` trusts only the
dedicated field. In both cases thinking tags are removed from visible assistant
content. Capture controls response parsing only and does not change the request.

## Capability discovery and protocol selection

`Provider(...)` construction is offline. Inspect explicitly when desired:

```python
capabilities = await provider.inspect_capabilities()
print(capabilities.model_dump())
```

The report uses `supported`, `unsupported`, and `unknown` for Chat, Responses,
reasoning, effort, output limits, tools, streaming, structured output, and `/tokenize`.
Discovery reads `/openapi.json` and `/version` once and caches the result.

Auto selection prefers Responses only when it supports every capability needed
by the current request, including tools, streaming, structured output, and
reasoning controls. Otherwise it selects Chat when Chat satisfies the request;
if neither discovered protocol does, it fails before issuing a POST. If
discovery is unavailable, it warns and selects Chat.
Setting `protocol="chat_completions"` or `"responses"` is strict: endpoint
failure is returned to the caller and never triggers cross-protocol replay of a
possibly side-effecting request.

A Responses generation is accepted only with terminal status `completed`.
`failed`, `incomplete`, and `cancelled` terminal states raise
`ProviderResponseError`. Chat Completions also raises this error when
`finish_reason="length"` reports output-limit exhaustion. Partial streaming
output is never converted into a successful run.

Each recorded `AssistantMessage.model_call` exposes the selected protocol,
request purpose, requested and effective effort/output limit, actual wire field,
and the auto-selection reason. This audit metadata is persisted with the
conversation but never projected back into model input.

## Token accounting and compaction

`token_counting="auto"` calls vLLM `/tokenize` for the projected messages and
tools when the endpoint is advertised. `ContextUsage.estimator` is then
`"vllm"`, and `server_max_model_len` records the discovered maximum. Set
`token_counting="vllm"` to fail when exact counting is unavailable, or
`"heuristic"` to always use the local deterministic estimate.

With `context_window_tokens=None`, the discovered `max_model_len` is the total
window. Discovery failure warns and falls back to 32,768. An explicit value
overrides discovery, but a value larger than the server limit is rejected before
generation. `max_output_tokens=None` sends no output-limit field. A configured
value maps to Chat's discovered `max_completion_tokens` or `max_tokens`, and to
Responses `max_output_tokens`.

For total window `W` and output limit `O`, the input budget is `W - O`; without
an output limit it is `W - 1`. Exact vLLM counts are not inflated. The safety
margin applies only to heuristic/custom counters.

Compaction is based on token pressure, not a minimum number of conversation
turns. At the configured trigger, dagent applies reductions in this order:

1. summarize old history while retaining a recent raw-history target of 16%;
2. omit the oldest replayed reasoning from the active request projection;
3. summarize completed middle steps of an oversized active run.

The current run's initiating user input, an open assistant/tool-result chain,
and the latest atomic step are retained. Tool-call/result pairs are not split.
The 16% retention target is soft: when fixed input would otherwise cause a hard
overflow, dagent summarizes additional oldest cross-run history first. If
required input still exceeds the effective window after reductions,
`ContextWindowExceeded` is raised before generation.

The default trigger is 80% of input capacity and is a soft threshold: after all
safe reductions, a request between the trigger and the hard input budget may
still run. Summaries default to at most 8,192 output tokens and use the separate
`compaction_reasoning_effort="low"`, regardless of the normal provider effort:

```python
context = dagent.ContextPolicy(
    compaction_trigger_ratio=0.8,
    compaction_retain_ratio=0.16,
    summary_max_tokens=8192,
    compaction_reasoning_effort="low",
)
```

Summary reasoning is discarded. If the summary call or its requested effort is
unsupported, dagent records the reason and uses the bounded deterministic
fallback without failing the agent run.

`ContextUsage` reports the replay mode, replayed and omitted reasoning counts
and token estimates, active-run compaction, exact/heuristic estimator, effective
window, and configured cap.

## Custom provider compatibility

Existing custom providers implementing `chat(...)` and optional
`stream_chat(...)` remain usable through an explicit internal adapter. They
receive the normal Chat message/tool shape, but provider-specific reasoning
replay is omitted because the SDK cannot infer their accepted input field.
Implementations that need dual-protocol behavior should use the built-in
private-vLLM `Provider`.
