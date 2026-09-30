# Model Context and Reasoning

For result display budgets, typed retention metadata and file/search pagination, see
[Tool Result Recovery](tool-result-recovery.md).

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
key. `"omit"` removes it. `"auto"` chooses `reasoning_content` for recognized
official DeepSeek V4 endpoints/models, `reasoning` for vLLM and `omit` otherwise.

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

A Responses generation is accepted only with terminal status `completed`; a
stream must include `response.completed`. Chat Completions must supply a valid
`finish_reason` (`stop`, `tool_calls`, or `content_filter` with an explicit
refusal). Missing termination, output exhaustion (`length`), and failed,
incomplete, or cancelled Responses raise `dagent.providers.ProviderResponseError`.
Its `reason`, `status`, and partial `response` are available to direct provider
callers. Invalid tool-call JSON raises the same typed error with reason
`invalid_tool_call`; reasoning text is never parsed into executable calls.

In a tool-agent run these errors return `RunResult.status="failed"` with a typed
`result.error` and empty `output_text`. Tool results, the last assistant response,
usage, and trace remain available. A completed generation containing only
reasoning or no content also fails (`reasoning_only_response` or `empty_response`).
Ordinary tool errors are still passed to the model for the next turn. A new valid
call executes normally. A nonempty final answer or explicit refusal ends the
loop. This contract validates a model turn, not whether the answer achieves the
user's task. Protocol failures do not rerun the task or previous tools, including
when a task validator is configured.

Each recorded `AssistantMessage.model_call` exposes the selected protocol,
request purpose, requested and effective effort/output limit, actual wire field,
and the auto-selection reason. This audit metadata is persisted with the
conversation but never projected back into model input.

The same metadata records the server's actual `finish_reason` and
`response_status`; absent values are `None`. Missing fields in `ModelTokenUsage`
are also `None`, while reported zero remains zero. `ModelCallMetadata.attempts`
contains public `ModelCallAttempt` records: attempt number (starting at one),
elapsed seconds, exception type, HTTP status, and delay before a retry. `None`
means an HTTP status was unavailable or no retry followed. Runner retries
transient model request failures using its existing policy; it stops retrying a
stream after tokens have been emitted. OpenAI client retries are disabled to
make every attempt observable. Direct provider calls make one attempt and raise
`dagent.providers.ProviderRequestError` on transport/HTTP failure, exposing
`metadata`, `cause`, and any partial `response`.

## Token accounting and compaction

`token_counting="auto"` calls vLLM `/tokenize` for the projected messages and
tools when the endpoint is advertised. `ContextUsage.estimator` is then
`"vllm"`, and `server_max_model_len` records the discovered maximum. Set
`token_counting="vllm"` to fail when exact counting is unavailable, or
`"heuristic"` to always use the local deterministic estimate.

`/tokenize` must return a nonnegative integer `count` or a list of integer token
IDs. Zero is rejected for nonempty messages/tools. Invalid structures and invalid
`max_model_len` values trigger a warning and explicitly labelled heuristic
counting in `auto`; explicit `vllm` raises `dagent.providers.ProviderTokenCountError`.
No missing count is silently interpreted as an exact zero.

`ContextUsage.context_window_source` identifies `configured`, `server`, `model`,
or `fallback` (old unobserved records use `None`). The fallback 131,072 is a local
budget, not a verified server capability. `server_max_model_len` stays `None`
until a valid probe supplies it. A later failed/missing probe retains a previously
verified limit while the estimator switches to heuristic counting. The provider
also exposes `context_window_source` and `server_max_model_len`.

With `context_window_tokens=None`, the discovered `max_model_len` is the total
window. Discovery failure warns and falls back to 131,072 (128K). An explicit value
overrides discovery, but a value larger than the server limit is rejected before
generation. `max_output_tokens=None` sends no output-limit field. A configured
value maps to Chat's discovered `max_completion_tokens` or `max_tokens`, and to
Responses `max_output_tokens`.

For the official DeepSeek API (`https://api.deepseek.com`, optionally with
`/v1` or `/beta`), dagent recognizes `deepseek-v4-flash`, `deepseek-v4-pro`, and
`deepseek-v4-flash-vision-exp` as having a 1M context window. It uses the
1,048,576-token limit in DeepSeek's [official model catalog example](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/),
checked on 2026-09-07. This is an endpoint-and-model lookup, not a live context
length response: DeepSeek's `/models` does not publish context limits. Unknown
models fall back to 128K; third-party endpoints do not inherit the official limits.
Explicit limits override the lookup but cannot exceed the known model limit.
Construction remains offline. DeepSeek `auto` counting uses the heuristic without
calling `/tokenize`; explicit `vllm` counting raises an error. `ContextUsage`
reports the known limit in `model_context_window_tokens`, separately from
`server_max_model_len` and `estimator`; model recognition does not imply exact
token counting. This also applies with `token_counting="heuristic"`.

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

## DeepSeek Chat tool replay

Since 0.9.11, `auto` recognizes HTTPS `api.deepseek.com` (default/443 port;
root, `/v1`, or `/beta` base path) with `deepseek-v4-flash`, `deepseek-v4-pro`,
or `deepseek-v4-flash-vision-exp`. Explicit field choices always win. Unknown
models and third-party endpoints do not inherit this protocol from a model name.

The [official thinking guide](https://api-docs.deepseek.com/guides/thinking_mode/)
requires full `reasoning_content` replay when requests carry `tools`, including
assistant turns without tool calls. Without tools, the service can ignore it.
Sources checked 2026-09-07: [models](https://api-docs.deepseek.com/),
[/v1 example](https://api-docs.deepseek.com/quick_start/agent_integrations/workbuddy/),
[beta tools](https://api-docs.deepseek.com/guides/tool_calls/).

This fixes field mapping only. `active_run` still excludes other Runs' reasoning,
and context pressure can still omit older reasoning independently of messages.
Those requests may not satisfy DeepSeek's full-replay requirement. `none` and
explicit `omit` are honored even when the endpoint may reject the resulting
tool request. This change does not prove that replay omissions caused repeated
file reads, or that a server used reasoning carried by a request.

A small opt-in check uses synthetic data and at most three generation requests:
`DAGENT_RUN_DEEPSEEK_TESTS=1 uv run python -m examples.deepseek_replay`.
Provide the test credential through `API_KEY`; the example prints counts and
validation results, not reasoning, requests, or credentials. The check uses
`reasoning_effort="high"`; an empty reasoning response cannot validate replay.
Each generation
has a 1024-token output limit and retries are disabled. Failure stops the check;
a truncated generation is a failed check, not a reason to retry automatically.

## Observe reasoning carried by a request

`ContextUsage.replayed_reasoning_items` / `replayed_reasoning_tokens` retain
their existing meaning: reasoning retained in the internal context projection.
The `omitted_reasoning_*` fields count projection omissions. Neither proves
that a Chat field was serialized. Historical counters are not redefined.

Since 0.9.11, `AssistantMessage.model_call.request_reasoning` (also available
on compaction model-call metadata) reports text from the final serialized HTTP
request, after `extra_request_args` and `extra_body` overrides:

| Field | Meaning |
| --- | --- |
| `resolved_field` | Provider mapping: `reasoning`, `reasoning_content`, or `omit`. |
| `serialized_fields` | Actual nonempty text fields; `("omit",)` when absent. Both Chat fields can be listed if a raw override supplies both. |
| `serialized_items` | Number of nonempty assistant reasoning fields, or Responses reasoning input items with text. |
| `serialized_characters` | Sum of Unicode code points after JSON decoding; excludes JSON escapes/wrappers and is not a token count. |
| `omission_reasons` | Context or serialization omissions; multiple reasons may coexist. |

Reasons are `policy_none`, `outside_active_run`, `context_budget`,
`explicit_omit`, `auto_unsupported`, `request_override`, and
`no_reasoning_available`. Context reasons describe the projection supplied to
the provider; raw overrides may subsequently supply different content.
Responses counts text in reasoning `content` and `summary`, once per item;
`reasoning.effort`, encrypted data, and generated response reasoning are excluded.
An empty reasoning string contributes zero.

```python
for item in result.conversation.items:
    if isinstance(item, dagent.AssistantMessage) and item.model_call:
        observation = item.model_call.request_reasoning
        if observation is not None:
            print(observation.model_dump(mode="json"))
```

`RequestReasoning` lives in `dagent.schemas.context`; it is not a package-root
export. Missing metadata means **unknown**, including old saved records and
providers that do not instrument their requests. It does not mean zero sent.
The summary travels with normal response metadata, including the final stream
result; it is not a separate failed-request or transport-attempt log. It adds
no reasoning text, complete request, or credentials to logs or persistence.
It establishes only what the request carried, not what the server used.
