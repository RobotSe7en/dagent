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
or `max`. By default the SDK sends it as `reasoning_effort` to Chat or
`reasoning.effort` to Responses. Model support is still determined by the model served by vLLM.
Token-based reasoning budgets are not part of the SDK contract.

`reasoning_capture="field_and_tags"` combines the dedicated response reasoning
field with `<think>` content. `reasoning_capture="field"` trusts only the
dedicated field. In both cases thinking tags are removed from visible assistant
content. Capture controls response parsing only and does not change the request.

### Custom reasoning fields and effort names

Since 0.9.19, use `reasoning_mapping` when an endpoint needs a different
request field or effort name. Each protocol has its own optional mapping:

```python
from dagent.config import ReasoningMapping

provider = dagent.Provider(
    base_url="http://localhost:8000/v1",
    model="private-model",
    reasoning_effort="high",
    reasoning_mapping={
        "chat_completions": ReasoningMapping(
            field="custom_reasoning.level",
            values={"high": "deep"},
        ),
        "responses": ReasoningMapping(
            field="reasoning.effort",
            values={"high": "xhigh"},
        ),
    },
)
```

The example fields and values must match your endpoint's contract. Chat sends
`{"custom_reasoning": {"level": "deep"}}`; Responses sends
`{"reasoning": {"effort": "xhigh"}}`. Plain dictionaries are also accepted in
Python. The equivalent configuration-file shape is:

```yaml
provider:
  base_url: http://localhost:8000/v1
  model: private-model
  reasoning_effort: high
  reasoning_mapping:
    chat_completions:
      field: custom_reasoning.level
      values: {high: deep}
    responses:
      field: reasoning.effort
      values: {high: xhigh}
```

`field` is a required, nonempty dotted JSON object path; arrays and SDK-managed
fields such as model, input, tools, output limits, and Responses state are
rejected. `values` defaults to an empty dictionary. Its keys are the existing
effort names, and its target values must be strings. Unlisted effort names pass
through unchanged. An unset effective effort sends no mapped parameter; the
string `"none"` remains a transmitted value.

Mappings apply to generation, streaming, the Chat entrypoints, and context
compaction. Compaction still selects its independent `low` effort by default,
so the example sends `low` for compaction. Add a `low` entry to translate that
value too. This does not change the compaction policy or `/tokenize` payload.

For a mapped protocol, the SDK trusts the explicit field/value configuration
instead of requiring OpenAPI to advertise the built-in effort controls. It
does not generate the usual effort field or `include_reasoning`; add any
required switches explicitly through extra parameters. Endpoint, tools,
streaming, structured-output, output-limit, and reasoning-replay checks still
apply. Capability inspection continues to report the server's declarations.

`auto` retains the existing protocol preference. An unconfigured protocol uses
the built-in behavior, even when the other protocol has a mapping. Set an
explicit protocol when only that protocol's mapping should be used. Omitting
`reasoning_mapping`, setting it to `None`, or using an empty dictionary preserves
the existing behavior.

Extra parameters retain their existing precedence. `extra_request_args` can
replace the mapped root object; `extra_body` overrides generated body values
and overlays SDK arguments in the final JSON. An explicit
`extra_request_args["extra_body"]` replaces the entire generated/configured body,
including mapped fields it does not repeat. Use `extra_body` for custom fields
that are not OpenAI client arguments.

For mapped calls, metadata keeps the original `requested_reasoning_effort` and
reads `effective_reasoning_effort` from the final serialized mapped field. A
missing or non-string value is recorded as `None`. `fallback_reason` includes
the configured field path. This reports what was sent, not whether the model
honored it. Rejected mappings do not trigger automatic downgrades, parameter
omission, or protocol switching.

Run the offline [reasoning mapping example](../../examples/reasoning_mapping.py)
with `uv run python -m examples.reasoning_mapping`.

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

## Prompt cache usage

Since 0.10.0, cache statistics are opt-in and provider-reported, independently of
the context estimator. Enable `capture_cache_usage=True` on `Provider(...)` or in the provider
configuration used by `Runner.from_config(...)`. The default is `False`.

```python
provider = dagent.Provider(
    base_url="http://localhost:8000/v1",
    model="local-model",
    capture_cache_usage=True,
    stream_include_usage=True,  # Only for endpoints supporting Chat streamed usage.
)
```

This option does not send a new request field, enable server caching, change
protocol selection, or automatically enable streamed usage. Chat reads
`usage.prompt_tokens_details.cached_tokens`; Responses reads
`usage.input_tokens_details.cached_tokens`, including terminal streaming
responses. Private fields such as `prompt_cache_hit_tokens` are not mapped.

`ModelTokenUsage.cached_input_tokens` is a subset of the existing `input_tokens`,
not an additional token charge. It is `None` when capture is disabled, the count
is absent, or the statistic is invalid; explicit `0` means a reported cache miss.
Missing statistics never imply zero hits. With capture enabled, negative,
non-integer (including boolean and numeric-string), or greater-than-known-input
counts are omitted with a warning containing no request or response text. Other
valid usage and generation results are preserved. A valid cache count can remain
known even when `input_tokens` is unknown.

Read per-response usage on `AssistantMessage.usage`, including planner audit
items in `result.new_items`. Compaction has its own `ContextSummary.usage` and
`model_call.request_purpose="compaction"`. Existing model-call trace outputs and
final result/checkpoint serialization retain these counts. `ContextUsage`
describes request estimates and projection, while `RunResult.usage` counts
operations; neither represents prompt-cache hits. Statistics are audit data and
are never replayed into model input.

```python
for item in result.new_items:
    if isinstance(item, dagent.AssistantMessage) and item.usage is not None:
        usage = item.usage
        ratio = (
            usage.cached_input_tokens / usage.input_tokens
            if usage.cached_input_tokens is not None
            and usage.input_tokens is not None and usage.input_tokens > 0
            else None
        )
        print(usage.cached_input_tokens, ratio)  # None means unknown/undefined.
```

The SDK supplies no run-level cache aggregate or serialized hit-rate field.
Hosts can compute ratios from known counts; exclude unknown calls explicitly
and report that coverage. Streaming usage snapshots replace earlier snapshots,
rather than being summed. A returned response's usage does not claim to include
all failed retry attempts.

Unknown cache counts are omitted from serialized usage, preserving the released
default record shape. Old records load without migration. Records containing
the opt-in field require a reader supporting it; old strict readers may reject
them. See the offline [cache usage example](../../examples/cache_usage.py).

Prefix reuse still depends on the actual model input and server cache state.
Fixed workspaces help preserve paths, while `active_run` reasoning omission,
context pressure, summaries, or reconstructed DAG planner history can change
prefixes. Enabling usage capture does not change those policies.

## Token accounting and compaction

This section describes the default `compaction_strategy="legacy"`. The opt-in
ToolAgent strategies described below use final-request counting and independent
summary budgets instead. Existing applications keep the behavior in this section.

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

Tool results and planner observations use the fixed per-result display limit.
Their budget is not redistributed when new results arrive or when context space
runs low. The separate total tool-result budget is deprecated and ignored;
overall context pressure triggers summaries of old complete exchanges. This
keeps retained displays stable for prefix caching. If protected results or other
required input still cannot fit, generation raises `ContextWindowExceeded`.
Configure the per-result limit to fit the model window and parallel tool batch size. See
[result recovery](tool-result-recovery.md).

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

`context.compaction.finished` reports actual history summarization, including a
deterministic fallback summary. Saving a tool result or shortening its display
alone does not emit that event; inspect the tool-result counters in `ContextUsage`.
SDK-created subagents and auxiliary roles inherit the provider's last verified
server window, including when exact counting later becomes unavailable. `auto`
still permits heuristic counting; a completely unknown window still uses the
warned 128K fallback. Explicit `vllm` counting remains strict.

## Bounded ToolAgent context strategies

Since 0.10.0, long SDK ToolAgent runs can explicitly select `"chunked"` or
`"cache_aware"`:

```python
provider = dagent.Provider(
    base_url="http://localhost:8000/v1",
    model="qwen-3.8-flash",  # Use the model name served by your deployment.
    context_window_tokens=131072,  # Use 262144 for a verified 256K deployment.
    max_output_tokens=8192,
    token_counting="auto",
    capture_cache_usage=True,
    stream_include_usage=True,
    timeout_seconds=600,  # Example per-request timeout for a slow local model.
)
agent = dagent.ToolAgent(
    profile="conversation",
    context=dagent.ContextPolicy(
        compaction_strategy="cache_aware",
        allow_reference_fallback=True,
        compaction_timeout_seconds=600,  # Optional total deadline per compaction episode.
    ),
)
```

Register the agent's required tools on `Runner` as usual. `chunked` uses dedicated
summary requests. `cache_aware` first tries a summary instruction appended after
an eligible, complete history prefix, retaining the original system instructions
and tools for that request. It falls back to dedicated chunks when the prefix
request cannot fit or is incompatible. The latest protected exchange is not
included merely to preserve its cached prefix. The fixed maintenance instruction
is present from the first `cache_aware` generation. Actual prefix reuse still
depends on the server's template, routing, and cache state; it is not guaranteed.
These strategies apply to SDK ToolAgent execution. DAG execution and the default
`legacy` strategy keep their released behavior. Custom providers must implement
the internal prepared-call contract to use the new strategies.

### One counted request and three budget thresholds

The provider freezes its actual request after applying extra-parameter precedence.
The same immutable payload is counted and sent, for normal generation and
compaction, including streaming. New strategies reject extra parameters that
replace the projected model, history, instructions, or tools with different
values. Server-side input truncation and stateful Responses chains are rejected.
Summary calls also reject fixed extra output schemas incompatible with their task
summary. Ordinary generation retains supported extra output-format overrides.

Let `W` be the effective total window and `O` the enforced output allowance.
New strategies use the smallest valid configured, discovered server, and known
model limit. A smaller server window shrinks the budget and refreezes automatic
output limits before generation; it does not enlarge the server's capacity.
Reasoning can consume `O`; it is not an additional allowance. The hard input
budget is `H = W - O`. The default soft trigger is `floor(0.8 * H)`. Compaction
tries to leave space near `floor(0.65 * H)` but accepts a legal request above that
target when mandatory input prevents further reduction. At the soft trigger,
`compaction_retain_ratio` keeps recent complete groups near its 16% default,
including at least the latest group. Under soft pressure, compaction also waits
until the selected older history is worth summarizing: about 10% of `H`, measured
as its share of the counted request. Otherwise mandatory input that stays above
the trigger would cost a paid summary and a prefix break on every step. Hard
pressure still compacts at once, and may compact this recent tail up to the
protected latest group. Every candidate is checked against `H` again before
generation; cache hits do not increase `H`.

| Verified total window | Output allowance | Hard input budget | Default soft trigger |
| --- | --- | --- | --- |
| 131,072 (128K) | 8,192 | 122,880 | 98,304 |
| 262,144 (256K) | 8,192 | 253,952 | 203,161 |

Unlike `legacy`, an unset normal output limit in the new strategies becomes an
explicit wire limit of `min(8192, W // 8)`. An explicit final extra output limit
retains its precedence and is included in the budget. `summary_max_tokens`
limits visible summary text; each summary request independently reserves at most
`min(2 * requested_summary_tokens, W // 4)` total output tokens, including any
reasoning. It does not inherit a smaller normal-generation limit. This allowance
does not guarantee that all requested visible summary text will be generated.
The requested summary size is reduced to fit the mandatory final generation input.

Counting prefers a detected vLLM `/v1/chat/completions/render` or
`/v1/responses/render` endpoint using the complete final payload. The returned
prompt token IDs provide the strongest available count. A compatible `/tokenize`
request is next; differences such as tool-choice rendering or unsupported
template options are never silently labelled exact. `auto` can use the local
estimate with `token_safety_margin` (15% by default); explicit `vllm` fails if no
supported server counting path is available. `ContextUsage.count_precision`
distinguishes `rendered`, `tokenized_estimate`, and `estimated`, independently of
`estimator`. `request_fingerprint` identifies the frozen body, not a cache key.

The local estimate never replaces an available server count. It can overcount
by more than its margin; CJK text is a common case. Generation candidates and
compaction requests, including the `cache_aware` warm-prefix request, are
therefore counted before the SDK compacts or splits them. A discovered server
window only lowers the effective window.

Strict budgets reject explicit `truncation=null` as well as automatic server
truncation; omit the field or use `"disabled"`. If rendering rejects an automatic
output reservation before returning a count, a supported tokenizer endpoint can
discover the model window separately. A window-only probe is not a count of the
generation request. The SDK rebuilds the automatic reservation and renders the
final request again; explicit output limits are never silently reduced.

Render availability is version- and deployment-dependent. In the
[verified vLLM source](https://github.com/vllm-project/vllm/blob/428cd0cb4a42750f5339d08bb67d534cf8051429/vllm/entrypoints/scale_out/render/api_router.py),
the render routes use the full generation request schema; an ordinary server
requires `--enable-scale-out` to expose the usable handlers. dagent probes this
capability and does not assume it exists. `/tokenize` is not a universal
replacement for rendering every generation option. Known over-budget candidates
are reduced or rejected before generation. Unknown counts, incompatible server
settings, or a changed deployment can still produce a server context error.
dagent does not classify arbitrary HTTP 400 responses as context overflow or
automatically replay generation based on error-message text.

Render validates input length and rejects an over-window body instead of
counting it. Such a rejection raises
`dagent.providers.openai_compatible.ProviderRenderRejected`, a subclass of
`ProviderTokenCountError`; existing handlers are unaffected. When the local
estimate already places that candidate over budget, the rejection is treated as
overflow. The SDK compacts the generation input, or splits the compaction
request, without sending the rejected request. A rejection of a candidate
estimated to fit remains a diagnostic failure. When the tokenizer then counts the
rejected request above the input budget, the count is reported as a
`tokenized_estimate`, also when tools make it approximate.

### Complete source coverage and stable result displays

Summary requests cover the selected source continuously, splitting oversized
sources into explicit ranges instead of dropping a middle section. Structured
summaries retain goals, constraints, decisions, completed work and relevant tool
observations, with pending work and source references. This helps the model avoid
forgetting completed steps; it does not deduplicate or suppress tool calls.
Raw received sources, summary coverage, and model-call usage are archived.

Each compaction episode permits at most eight logical model calls and twelve
model request attempts, including retries. These limits do not cap normal agent
generation or the total task duration. Summary text is collected internally from
a stream and is not emitted as the agent's answer. `compaction_timeout_seconds`
defaults to `None`, adding no total deadline beyond the provider's request
timeouts. Set it explicitly to bound the entire episode, including retries;
increase the provider timeout separately for slow self-hosted models. Cancellation
still propagates. Repeated unchanged failed sources are not resummarized every
turn, and nonconverging replacements cannot create an unlimited summary loop.
A source is recorded as failed only when the summarizer was reached: an invalid
or oversized summary, an exhausted episode budget or deadline, or a terminal
re-preparation signal. A transport failure that persists after request retries
(connection error, timeout, HTTP 408/409/425/429 or 5xx) with no completed chunk
is not a verdict on the source. That episode stops calling the summarizer and
records nothing; a later preparation or continuation retries the same source.
Conversations saved by `0.10.0` through `0.10.2a0` may already mark such a
source as failed. To retry it, continue from
`conversation.model_copy(update={"compaction_state": None})`, or enable
`allow_reference_fallback=True`.
If the final count still exceeds the hard budget, the same episode can tighten
only its current root summary once, without resending the original history.

New tool results share the remaining batch budget before their first generation
display. In these strategies, `max_tool_result_tokens` caps each result's normal
body allowance; required identity/status/recovery metadata is additional. A
persisted receipt freezes the exact text, shown ranges, and recovery cursor.
Later steps, review continuations, and new runs reuse that text without repeatedly
redistributing the budget. `max_total_tool_result_tokens` remains deprecated and
ignored. Under otherwise unresolved hard pressure, the latest already frozen
batch can make one explicit transition to reference-only display, recorded as
`prefix_break_reason="emergency_result_reference"`. All calls, arguments,
statuses, and corresponding result messages remain present.

Human review feedback is retained independently of an externalized result body.
It is required display metadata, including after emergency shortening, rather
than part of the body's truncatable allowance or its character offsets.

Shortening requires the original received text to be readable through the
agent's existing boundary-checked recovery tool. Tools are not silently added.
Unreadable result bodies stay intact, including NUL text that the built-in file
reader cannot recover. Budget pressure can then require an explicit failure.
Recovery restores what the SDK received;
upstream source truncation cannot be undone. The active user instructions and
complete latest call/result relationships are protected. Oversized system
instructions, tool schemas, user input, call arguments, or minimum result
metadata can therefore still require `ContextWindowExceeded`.

An older frozen display with hidden text also requires working recovery. If its
reader or source becomes unavailable, preparation fails without rewriting the
receipt; a display that already contains the complete body remains usable.

`allow_reference_fallback=False` preserves raw source when complete semantic
summarization fails. A request that still fits may continue. Setting it to `True`
also permits lower-fidelity reference continuation when necessary to fit: only
recoverable source is replaced, unresolved coverage stays explicit, and the final
hard check still applies. `ContextSummary.method="reference_fallback"` and the
archived record's `partial` or `reference_only` status distinguish this from a
complete summary. Neither mode promises lossless semantics or zero context errors.

Old partial summaries are reused only for matching input. If another compaction
changed the root, enabled reference fallback retains the current source and
explicit unresolved ranges without repeating a model request for that source.

A runtime archive index reuses verified reference links across preparations.
Every traversal checks file identity; unchanged historical bodies need not be
reread, rehashed or reparsed. Changed files are reverified; missing or corrupt
files still fail verification. A new runtime or workspace verifies its history
afresh. Body I/O is reduced,
while file-state traversal remains proportional to retained history. The index
retains links and file identities, not all historical bodies.

### Failure snapshots and cache verification

For a new-strategy context preparation failure, the original exception keeps its
type and exposes a failed `result` with the latest executed tool batch. A resolved
execution configuration also supplies a checkpoint through the result and
`Runner.run_checkpoint(run_id)`. If a later server discovery invalidates an
explicit output limit, this snapshot keeps the last valid generation configuration;
initially invalid configuration has no resolved checkpoint.
Preparation/counting failures after a successful summary also retain the paid
record. A new run can reuse complete results for matching input, but must pass
the final budget check before committing them. Original error types and
cancellation still propagate.
After resolving the size or capability problem, start a **new run** with
`Runner.run(agent, input="Continue from the saved progress.",
conversation=error.result.conversation)`. This is conversation continuation, not
same-run resume or an automatic replay of completed tools. Keep the source
workspace and persisted resources available. Missing or corrupt recovery files
are explicit failures; a reference is not a substitute for retained source data.

For vLLM cache validation, configure the server's prefix caching and
`--enable-prompt-tokens-details`; the latter controls whether standard
`cached_tokens` is reported in the
[verified vLLM implementation](https://github.com/vllm-project/vllm/blob/428cd0cb4a42750f5339d08bb67d534cf8051429/vllm/entrypoints/openai/chat_completion/serving.py).
Use `capture_cache_usage=True`; for Chat streams also request usage with
`stream_include_usage=True` when supported. Pin two sufficiently long identical
requests to the same pod, inspect their provider-reported cached counts, and
compare cold and warm requests. Missing statistics mean unknown. The SDK does not
enable server flags, clear production caches, or promise a hit. Track uncached
input plus summary input/output and latency, since a higher hit ratio alone does
not establish a lower total cost.

Default-valued new policy and audit fields are omitted from saved records so old
default checkpoints retain their fingerprints. Records using the new strategies,
frozen displays, or summary manifests require a reader supporting those fields.

Run the offline [context management example](../../examples/context_management.py)
with `uv run python -m examples.context_management`. For a real deployment, the
[vLLM soak harness](../../examples/vllm_context_soak.py) defaults to a one-hour,
200-tool-call, five-compaction acceptance target:

```bash
uv run python -m examples.vllm_context_soak --live \
  --base-url http://localhost:8000/v1 --model MODEL --context-window 128 \
  --workspace /tmp/dagent-vllm-soak --report /tmp/dagent-vllm-soak.json
```

Use `--context-window 256` for a verified 256K deployment. This harness requires
usable server counting (`token_counting="vllm"`); enable the render handler when
`/tokenize` cannot mirror the deployed generation options. The operator supplies
the matching tool template, APC, usage details, and any required API-key
environment variable. Without `--live` it sends no requests. Missing cache usage
or reduced smoke-test targets cannot count as a full acceptance pass. Reports
include observed budgets, usage and progress; conversation/checkpoint files keep
the synthetic task's recoverable state. The harness never resets a shared cache.

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

## Provider wrappers and observations

Provider wrappers can use the supported `dagent.providers.ModelRequest`,
`PreparedModelCall`, and `ProviderReprepareRequired` contracts. A wrapper may raise
`ProviderReprepareRequired(cause, metadata=call.metadata)` from `count_prepared`,
`complete_prepared`, or `stream_prepared` after recognizing an explicit request
rejection and updating its preparation strategy, before emitting response events.

For the opt-in `cache_aware` and `chunked` ToolAgent strategies, the SDK invokes
the original request preparer with the same projected `ModelRequest` and stream
mode, counts the replacement, and checks its **effective wire output limit** and
the effective context window, including limits discovered during counting.
The runtime sends that same checked `PreparedModelCall`; the wrapper must not
silently replace it during dispatch. If it exceeds the budget, the existing
context strategy compacts or fails before sending it. Completed tools and paid
summaries are retained; neither tools nor the entire Run are replayed.

This also applies when counting a replacement requested during generation, to
the final forced count of a protected candidate, and to initial and replacement
compaction requests. Each counting chain allows at most four replacements
(five count attempts) and rejects unchanged or repeated payload fingerprints,
raising the triggering `ProviderReprepareRequired`. Dispatch retains its existing
four-change limit and fingerprint checks. Summary generation attempts still
share the twelve-attempt compaction budget; preparation and counting remain
within the compaction deadline. Ordinary `ProviderTokenCountError` and other
errors retain their existing handling and do not request re-preparation.
If counting re-preparation terminates during compaction, completed chunks are
saved in a partial record linked from the failed conversation for explicit
reference recovery. The typed signal still propagates when fallback is enabled.
Replacement overflow failures include that request's `ContextUsage` in the
failed result, including streaming results.

Rejection classification, parameter fallback rules (such as `high` → `medium`),
and rejection-cache scope/lifetime belong to the host wrapper. The SDK provides
no model-specific downgrade or automatic HTTP 400 retry policy. Register the
wrapper with `Runner(provider=wrapper, ...)`, delegate preparation to the native
provider using the wrapper's current strategy, and raise the signal only after
changing that strategy. Direct provider calls do not run this SDK budget loop.
See the runnable, offline [prepared-count re-preparation example](../../examples/prepared_count_reprepare.py):

```bash
uv run python -m examples.prepared_count_reprepare
```

The example recognizes one synthetic render rejection, caches the decision in
the wrapper, and selects `medium` on the next preparation without changing native
provider configuration. Unrecognized errors propagate unchanged. Existing
applications and wrappers need no changes unless opting into this signal.

`ModelRequest(omit_reasoning_effort=True)` explicitly omits strength controls,
including the selected mapping field and standard strength fields in extra
arguments. It does not inherit the provider strength. Other nested settings,
including explicit thinking switches, are retained; provider configuration is
never modified. Omission is opt-in and does not itself add retries or change
reasoning mappings or protocol selection.

`Runner(context_observability=True)` enables ToolAgent model-call observations
and context-maintenance progress. `model.call.observed` contains a unique request
ID, purpose, available usage, metadata, status, and elapsed time, without prompt or
response text. Failed requests may have unknown usage. `context.compaction.progress`
reports actual internal summary progress, throttled to once per second, and
`context.compaction.ended` closes the episode even on failure or cancellation.
`context.compaction.finished` continues to mean an actual context replacement.
No new events are emitted unless observation is enabled. Existing SDK defaults,
legacy compaction defaults, schemas 4/6/9 and default checkpoint fingerprints are retained.
Scoped emitters' attribution fields do not change these observation payloads;
use response and capability events for parent-capability, DAG, and node attribution.
With observation enabled, legacy summaries stream internally for progress;
their fallback policy is retained.

Streaming context failures now include the typed failed result already attached
to the original exception, preserving the same evidence as non-streaming calls.
This does not add same-Run or cross-restart recovery.
