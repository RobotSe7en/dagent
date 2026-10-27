# Migration Notes

dagent has released public SDK contracts. This page records user-facing changes
that may require action when upgrading.

## Current Release Line

The current stable package version is `0.10.3`.

## 0.10.3

Install with `pip install --upgrade 'dagent-ai==0.10.3'`.

### Reasoning capability discovery

This release applies the maintainer-approved reasoning checks directly, without
a new configuration switch. Chat effort depends on `reasoning_effort`, while
history replay depends on the selected assistant message field. Gateways that
declare effort but omit top-level `reasoning` and `include_reasoning` no longer
fail the effort preflight. An explicitly configured replay field may be sent
when an open or incomplete message schema leaves support unknown; the capability
report stays unknown and call diagnostics mark it unverified. Explicitly forbidden
fields still fail before generation, including compatibility Chat entrypoints.
Removing effort or using `"none"` does not itself delete stored reasoning.

`ProtocolCapabilities` adds optional `replay_reasoning` and
`replay_reasoning_content` fields, both defaulting to `unknown`. Existing
constructors remain valid. Consumers validating capability reports with their own
closed schemas should accept these two fields. Existing conversation/checkpoint
formats, replay policies and response parsing are unchanged; no persisted-record
migration is needed.

The SDK only auto-adds `include_reasoning=True` where the endpoint explicitly
declares a compatible field. Previously it was generated for built-in Chat effort
and vLLM Responses effort regardless of that declaration. Explicit extra
parameters keep their precedence. A service that needs this switch but omits it
from OpenAPI should declare it or configure `extra_body={"include_reasoning": True}`.
Mapped controls continue not to generate it. Responses no longer treats this
response switch alone as evidence for its `reasoning` object.

These corrections can change protocol selection or turn a preflight rejection
into a request that the service may reject. They do not add effort downgrades,
history omission or cross-protocol retries. Existing recognized service field
selection remains unchanged; unknown gateways should explicitly configure their
replay field. See [Model Context and Reasoning](model-context-and-reasoning.md)
and run `uv run python -m examples.reasoning_gateway` for an offline example.

Verification covers wire-level reasoning controls, open/closed and composed
message schemas, streaming, compaction and tool continuation, plus the complete
SDK test suite. Actual model support and gateway forwarding still require
deployment-specific verification; no live model requests are part of this release.

## 0.10.2

This stable release includes the provider counting, failure-handling, and
observation fixes introduced in [0.10.2a0](#0102a0), plus the context-maintenance
changes below. Install with `pip install --upgrade 'dagent-ai==0.10.2'`.

### Compatibility and upgrade

Public SDK signatures and persisted record formats remain compatible; no source
or record-format migration is required. The previously approved automatic output
budget and summary wire protocol changes affect the opt-in `chunked` and
`cache_aware` strategies. Deployments needing the previous 8K generation allowance
can set `max_output_tokens=8192`; custom summary services and test doubles should
adopt the Markdown protocol described below. The `legacy` strategy retains its
released behavior.

### Counted summary budgets and 16K automatic generation output

Compared with `0.10.2a0`, the maintainer-approved budget changes apply directly to
`chunked` and `cache_aware`; no new policy switch is required. The automatic
generation output limit is now `min(16384, max(1, W // 8))`, previously capped at
8192. At 128K and 256K this reserves 8192 more output tokens, reduces the input
budget and can trigger compaction earlier. Explicit output limits and extra
argument precedence are unchanged. Set `max_output_tokens=8192` on those large
window deployments to retain the previous generation allowance; this does not
restore the old summary algorithm. Smaller windows retain their proportional
automatic output allowance. `ProviderConfig.max_output_tokens=None`, non-strict
calls and `legacy` behavior are unchanged.

Summary planning counts the retained request instead of subtracting raw source
estimates from a differently measured total. It reserves space for the checkpoint
wrapper, balances the visible goal with reasoning/output allowance, and can
reduce spare warm-prefix output before using chunks. The automatic summary
output cap remains a quarter of the window. `summary_max_tokens` still defaults
to 8192 and accepts values down to 64. A small root can still tighten once.
Pure preflight capacity skips do not permanently fail a source; changed
projections can be reconsidered within the same bounded episode.
Under hard pressure, `allow_reference_fallback=True` still permits recoverable
reference continuation when preflight cannot fit a summary, for both history
compaction and root tightening. No additional configuration is required.

This changes summary prompts, requested output limits and possibly prefix/chunk
selection, latency and cost. Planning usually adds one count per candidate, or a
second count for separated spans. Larger summary budgets do not promise fewer
compactions or enough reasoning for every model. Verification uses deterministic
regression and offline endurance tests; real-model reasoning quality and cache
benefits require separate deployment testing.

Public signatures, exports, configuration defaults other than automatic strict
output, and persisted record schemas are unchanged. Existing records remain
readable and paid replacements remain reusable. Existing failed-source markers
are not automatically cleared. To explicitly retry previously blocked history,
use `conversation.model_copy(update={"compaction_state": None})`; clearing that
state also discards its paid-record reuse entries, so do so only intentionally.
No configuration or record format migration is required.

### Markdown compaction checkpoint protocol

For the opt-in `chunked` and `cache_aware` strategies, summary request instructions
and model output have changed from JSON with real source IDs to fixed-heading
Markdown with per-compaction aliases (`S0` normally identifies the previous
checkpoint). The maintainer approved this provider wire payload change. Custom
summary services and test doubles that depend on the old JSON protocol must return
entries such as `### Next Step\n- Continue the task. [S1]` using the supplied aliases.
JSON output is no longer parsed into structured entries; its lines are kept as entry
text. Only output without any entry gets one correction retry within the existing
episode budget.

Cited entries now finish before trailing commentary, and literal `</think>` text
inside checkpoint data is preserved. Aliases avoid current and inherited real
source IDs, including lowercase and zero-padded variants, so accepted real-ID
citations retain their source attribution. The previous-checkpoint alias in the
instructions and source positions follows the assigned name when `S0` is reserved.
Custom summary services should use the aliases supplied in each request.

Checkpoint parsing is lenient. Heading variants (case, numbering, separators,
singular or plural forms, and common Chinese names), headings with inline entries,
lines without bullets, and citation variants (full-width brackets or commas, `、`,
`;` or space separators, trailing punctuation) are accepted. Entries are no longer
discarded for lacking a known heading or citation: entries before the first known
heading join Completed Work, entries after an unknown heading stay in the current
section, and an uncited entry cites the first and last sources of its own request.
Commentary lines are kept as entries. This avoids correction retries and
failed-source markers caused by format drift; the retry budget is unchanged.

Compaction record schemas, `task_summary` fields and real source IDs, and summaries
rendered into context are unchanged. Existing records remain readable. No record
or configuration migration is required; the `legacy` strategy is unchanged.

### Context maintenance fixes for `chunked` and `cache_aware`

Except where noted, these fixes apply only to the opt-in `chunked` and
`cache_aware` ToolAgent strategies; `legacy` context behavior is unchanged.
Public exports and persisted record schemas are unchanged. No source,
configuration, or record migration is required.

- A summarizer transport failure that remains after request retries (connection
  error, timeout, HTTP 408/409/425/429 or 5xx) no longer marks the selected
  history as permanently failed, even after some chunks completed. The episode
  stops calling the summarizer, and a failed warm prefix is not retried through
  chunks. Within the Run, only hard pressure retries that source; a later Run or
  continuation retries it too. `compaction_reason` keeps its `CompactionError:`
  prefix. Invalid or oversized summaries, exhausted budgets or deadlines, and
  terminal re-preparation signals are still recorded. Conversations saved by
  `0.10.0` through `0.10.2a0` can retry a span that an outage blocked by
  continuing from `conversation.model_copy(update={"compaction_state": None})`.
- Generation candidates and compaction requests are now counted by the server
  unless their local estimate exceeds twice the available input. Before, CJK-heavy
  input could be estimated over the hard budget while rendering only about 60% of
  it, which forced a hard compaction of almost all history. A render rejection of
  a candidate already estimated over budget raises the new
  `ProviderRenderRejected` subclass of `ProviderTokenCountError`, and the SDK
  compacts or splits instead of failing. A rejection that the tokenizer counts
  within budget remains a diagnostic `ProviderTokenCountError`. A tokenizer count
  above the budget for a rejected request is reported as an estimate, even with
  tools. A count that reports a larger `max_model_len` no longer enlarges a
  smaller window discovered earlier in the Run.
- Under soft pressure (above the trigger but within the hard budget), compaction
  now waits until the eligible older history is about 10% of the hard input budget.
  Before, mandatory input that stayed above the trigger caused one small paid
  summary and one prefix break per step. In an offline 61-step run with a large
  system prompt, summaries dropped from 57 to 19, and generation requests that
  kept the previous prefix rose from 3 to 41. Hard pressure still compacts
  immediately.
- Summary requests now state their size limit, in tokens and approximate
  characters, and ask the model to cite a previous checkpoint by its supplied
  alias. This changes the instruction text sent in opt-in compaction requests. An
  oversized checkpoint is retried once, within the episode's call budget, before
  it fails. Citations copied from inside the previous checkpoint resolve to it.
  Other unknown citations are dropped; entries without a known citation use
  their request's first and last sources, as described above. Before, cumulative
  summaries in long runs eventually exceeded an
  unstated limit, and a citation copied from the previous checkpoint rejected
  the whole paid summary. Both cases ended the run with `ContextWindowExceeded`.
- `cache_aware` now checks the provider-reported cache hit of each warm-prefix
  summary when `capture_cache_usage=True` (Chat streaming also needs
  `stream_include_usage=True`). After two consecutive hits below 50%, the rest
  of the Run uses chunk summaries. Prefixes that begin with a summary created in
  the same episode are not measured. Unknown usage changes nothing. Templates such as Qwen3 drop earlier
  reasoning once the summary instruction follows it. With such templates, use
  `reasoning_replay="none"` or `chunked`; see
  [Chat templates that drop earlier reasoning](model-context-and-reasoning.md#chat-templates-that-drop-earlier-reasoning).
- Saving and restoring conversation resources at Run boundaries no longer
  rereads unchanged history. Before, every continued Run reread and rehashed all
  retained results twice when saving and twice when restoring. In an offline
  benchmark, 60.8 MiB of retained results caused 121.7 MiB of reads per save or
  restore; repeats now read nothing. Runs of one `Runner` share the store and its
  bounded cache of verified file identities and compaction record links. Changed,
  missing, or corrupt files still fail verification. This applies to every
  strategy.

### Verification and known limitations

Validation: 2,059 Python/MCP tests passed, 3 skipped (two opt-in live MiniMax tests
and one real Docker test), with one expected tokenizer fallback warning. Both
offline context-management and prepared-count re-preparation examples passed.
Wheel/sdist builds, package metadata checks, and an isolated Python 3.13 wheel
installation with bundled-resource checks and a Runner execution passed.
Render rejection and endurance coverage use simulated endpoints; real-model
reasoning quality, live vLLM acceptance, and cache benefits require deployment
testing.

## 0.10.2a0

This prerelease supersedes `0.10.1a0` so its version follows stable `0.10.1`.
Applications pinned to the earlier prerelease should update to `0.10.2a0`.

### Re-preparation during prepared token counting

Provider wrappers may now raise `ProviderReprepareRequired` from `count_prepared`
after recognizing a request rejection and updating their preparation strategy.
The opt-in `cache_aware` and `chunked` ToolAgent paths prepare and count a new
snapshot, check its actual output limit and effective context window, and send
that same snapshot. This includes counting after generation-stage re-preparation
and compaction requests. Counting chains permit at most four replacements and
reject unchanged/cyclic fingerprints with the original typed signal.

Ordinary counting failures, existing generation re-preparation, default context
behavior, and public signatures/exports are unchanged. There is no tool or Run
replay. Host wrappers own rejection detection, fallback rules, and rejection
caches; no application migration is required. See
[Provider wrappers](model-context-and-reasoning.md#provider-wrappers-and-observations)
and the offline `examples.prepared_count_reprepare` example.

### Failure and observation fixes

- Scoped observation events from registered subagents and DAG ToolAgents no
  longer fail strict event validation. The adapter explicitly handles the
  emitters' attribution fields without changing observation payload schemas;
  response and capability events retain parent/DAG/node attribution.
- A terminal counting re-preparation signal now saves completed compaction
  chunks and attaches their partial record to the failed conversation. Explicit
  reference recovery can reuse that paid work without new model calls. The
  original typed signal still propagates, even when reference fallback is enabled.
- Generation replacement overflow failures include the overflowing request's
  `ContextUsage` in the failed result, including streaming results.

### Installation and verification

This is the alpha prerelease for `0.10.2`: `0.10.1 < 0.10.2a0 < 0.10.2`.
Install this prerelease explicitly:
`pip install 'dagent-ai==0.10.2a0'`. There are no breaking changes or required
source, configuration, or persisted-record migrations.

Validation: 1,872 Python/MCP tests passed, 3 skipped, with one expected tokenizer
warning. The offline prepared-count wrapper example, wheel/sdist builds, package
metadata checks, and isolated wheel installation passed. Render rejection tests
use simulated endpoints; this release does not claim live vLLM acceptance.

## 0.10.1

### Provider extensions and context observations

Provider wrappers can use the supported `dagent.providers.ModelRequest`,
`PreparedModelCall`, and `ProviderReprepareRequired` contracts. A wrapper may
request re-preparation only after an explicit request rejection and before any
response events. The ToolAgent budget owner freezes and counts the replacement
before dispatch, retaining completed tools and paid summaries. Re-preparation is
bounded to four changes per call and rejects repeated payload fingerprints.
Summary attempts share the existing twelve-attempt compaction budget and deadline.

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
legacy compaction, schemas 4/6/9 and default checkpoint fingerprints are retained.

Streaming context failures now include the typed failed result already attached
to the original exception, preserving the same evidence as non-streaming calls.
This does not add same-Run or cross-restart recovery.

## 0.10.0

This release adds opt-in long-task context management and prompt-cache usage
capture. Existing applications keep the released defaults; enabling either
feature is an explicit configuration choice.

### Context-management correctness fixes

The opt-in strategies retain review feedback for externalized results, preserve
paid summaries after preparation/counting failures, and allow reference fallback
after the root changes. NUL text is not shortened through an incompatible file
reader. A runtime archive index avoids repeated body reads/parsing while checking
file identity on every walk.

Strict budgets reject explicit `truncation=null`; omit it or use `"disabled"`.
Server window discovery can rebuild automatic output reservations after render
rejects them, including tool-bearing Chat and Responses requests. Window probes
are not generation-input counts. Explicit output limits and unrelated HTTP 400
errors keep their meaning. Default legacy behavior and existing checkpoints
require no migration.

### Opt-in bounded ToolAgent context management

`ContextPolicy.compaction_strategy` adds `"chunked"` and `"cache_aware"`; its
default `"legacy"` preserves existing SDK behavior, including DAG execution.
New strategies count and dispatch one frozen final provider payload, reserve an
enforced generation output limit, and use independent bounded summary requests
with continuous source coverage. If the normal output limit is unset, only these
strategies send `min(8192, context_window_tokens // 8)`. Different extra overrides
of projected history, model, instructions, or tools are rejected in the new modes;
legacy precedence remains supported.

New tool-result display receipts preserve the initial budgeted excerpt across
steps, review, and conversation continuation. New modes interpret
`max_tool_result_tokens` as a body allowance in addition to required metadata;
the deprecated total-result setting remains ignored. One explicit emergency
transition of the latest batch to references is allowed only with readable raw
source. All call/result relationships remain intact. `allow_reference_fallback`
defaults to `False`; enabling it permits explicitly recorded partial or
reference-only summaries when semantic summarization cannot make the request fit.

One compaction episode is limited to eight logical calls and twelve model request
attempts. `compaction_timeout_seconds=None` adds no total deadline beyond provider
timeouts; an explicit value bounds the episode, not the whole task. Prepared
vLLM counting prefers discovered render endpoints, then compatible `/tokenize`,
then labelled estimates in `auto`. New modes use the minimum configured/server/model
window and shrink automatic output limits after a smaller valid server discovery;
legacy configuration validation remains unchanged. No automatic HTTP 400 replay is added.
Known oversized requests are stopped before generation; unknown counts and
oversized mandatory input still cannot guarantee zero context errors.

Context preparation failures preserve the latest executed tools in a failed
result/checkpoint attached to the original exception. Continue through a new
`Runner.run(..., conversation=error.result.conversation)` after resolving the
cause, retaining its recovery resources. Existing review-resume behavior is
unchanged. Default new fields are omitted to preserve old default checkpoint
fingerprints; opt-in records require a compatible reader. No old-record migration
or new same-run continuation API is required.
Initially invalid configuration has no resolved checkpoint; its failed result
still retains conversation state. Later window-discovery failures keep the last
valid generation configuration in the checkpoint.

See the [configuration, 128K/256K budgets, recovery boundaries, and vLLM cache
verification guide](model-context-and-reasoning.md#bounded-toolagent-context-strategies).
Render availability and standard cache statistics require the appropriate server
version and flags; usage capture alone does not enable caching. Live self-hosted
cache performance and server context-error rates have not been benchmarked.

Verification: 1,804 Python tests passed with three environment-dependent skips.
Coverage includes 200 tool executions with at least five compaction commits,
Chat/Responses streaming, recoverable failure paths, and an unchanged checkpoint
generated by commit `8c2e9f2`. New regressions cover externalized review feedback,
failed paid-summary chains, stale-root fallback, small-window discovery, NUL text
and changed archive files. A 200-compaction synthetic benchmark reduced cumulative
archive body reads from about 1.25 GB to 25 MB; file-state walks remain linear per
preparation. The offline context example, documentation snippets,
and `git diff --check` passed. The live vLLM hour-long soak requires an endpoint
and has not been run.

### Opt-in prompt cache usage

`Provider(...)` and `ProviderConfig` accept `capture_cache_usage=False`. Enabling
it reads standard Chat/Responses cache statistics into the optional
`ModelTokenUsage.cached_input_tokens` field, including streaming and compaction.
It adds no request field and does not enable streamed usage or server caching.
Malformed cache counters are omitted with a warning, preserving generation and
other valid usage. Provider-private cache fields and run-level aggregation are
not included.

WebUI user models can set `capture_cache_usage: true` in `~/.dagent/config.yaml`;
model selection, editing, and saves preserve this option.

Defaults, reasoning replay, workspace selection and context projection are
unchanged. Unknown cache counts are omitted from serialized usage, including
nested conversations, summaries and checkpoints. Old records require no
migration; new records with a known opt-in count require readers that support
the field. Outer schema versions remain unchanged.
See [prompt cache usage](model-context-and-reasoning.md#prompt-cache-usage)
and the offline [example](../../examples/cache_usage.py).

### Release verification and upgrade

Install with `pip install --upgrade dagent-ai==0.10.0`. Upgrading from 0.9.19
requires no source, configuration, or stored-record migration. There are no
breaking SDK changes. The default context strategy remains `"legacy"`, and cache
capture remains disabled. Records produced with the new opt-in fields require
a 0.10.0-compatible reader; do not downgrade such records to an older reader.

Known limitations: summaries are lossy; oversized mandatory input and unknown
server limits can still cause context errors. Prefix-cache hits depend on the
deployment, tokenizer and server cache state. Real vLLM cache hit rates and the
hour-long live soak were not benchmarked for this release.

- Verification: 1,804 Python/MCP tests passed on Python 3.11, with three
  environment-dependent skips; 128 WebUI tests and the production build passed.
- Wheel and source distribution builds, package metadata checks, an isolated
  installed-wheel smoke test, both offline cache/context examples, and
  `git diff --check` passed.

## 0.9.19

### Optional reasoning field and effort-name mappings

`Provider(...)` and `ProviderConfig` accept `reasoning_mapping`, with independent
Chat and Responses field paths and string effort-name overrides. Unlisted values
pass through; protocols without a mapping retain their built-in behavior. The
same mapping applies to generation, streaming, Chat entrypoints, and compaction.
Compaction retains its independent `low` default.

Mappings are opt-in. Existing applications need no source or config changes;
existing request defaults, extra-parameter precedence, and
result/event schemas are unchanged. A mapped protocol trusts the explicit effort
configuration instead of the built-in effort capability checks and stops adding
automatic reasoning control fields. Other capability checks remain enforced.

To adopt, configure your endpoint's field path and any differing effort names as
described in [reasoning controls](model-context-and-reasoning.md#custom-reasoning-fields-and-effort-names).
Target values are strings only; this adds no token-budget API, automatic
downgrade, or omission retry. An unconfigured protocol can still be selected by
`auto` and uses its normal built-in controls. No persisted-record migration is
required; existing model-call metadata carries mapping diagnostics.

WebUI user models pass mappings to the provider and preserve them when selecting,
saving, or reloading models. The model API accepts the optional configuration and
includes a nullable `reasoning_mapping` field in model responses.

### Release verification and upgrade

Install with `pip install --upgrade dagent-ai==0.9.19`. Upgrading from 0.9.18
requires no source, configuration, or stored-record migration. This release adds
optional configuration and introduces no breaking SDK changes.

Known limitations: mapped field paths and effort names must match the endpoint's
contract; capability discovery does not validate custom reasoning controls. With
`auto`, an unmapped protocol can still be selected and uses its built-in behavior.

- Verification: 1,526 Python/MCP tests and 128 WebUI tests passed; the WebUI build,
  offline reasoning-mapping example, wheel and source distribution builds,
  package metadata checks, installed-wheel smoke test, and `git diff --check` passed.
- Three environment-dependent Python tests were skipped.

## 0.9.18

### Stable tool-result displays for prefix caching

**Breaking behavior change:** tool results and planner observations each use the
fixed `max_tool_result_tokens` limit. Appending new results no longer redistributes
the total budget or progressively shortens earlier results. Context-window
pressure no longer causes an additional request-local display reduction.

`max_total_tool_result_tokens` is deprecated and ignored. The field remains
accepted and serialized for saved policies, but no longer imposes a separate
aggregate cap or constrains the per-result limit. Only overall context pressure
triggers summaries of old complete exchanges. The latest atomic exchange stays
protected. If required input still exceeds the model window after normal history
and reasoning reductions, `ContextWindowExceeded` is raised before generation.

Migration: choose a per-result limit that fits the model window together with the
expected parallel tool batch, system instructions, tools, user input and reserved
output. A large per-result limit is no longer automatically reduced to fit. The
default values and public signatures remain unchanged; no durable schema or
stored-record migration is required. Historical displays can change once on
upgrade to this policy; subsequent appends preserve retained result text while
the result, policy, counter and recovery availability remain unchanged. Summaries
and reasoning omissions still change the corresponding prompt prefix. Storage,
structured values and permission boundaries retain their existing behavior.

### Release verification and upgrade

Install with `pip install --upgrade dagent-ai==0.9.18`. Apply the per-result budget
guidance above when upgrading from 0.9.17. Public signatures, request shapes,
capability ids, default configuration values and persisted schema versions remain
unchanged; no stored-record migration is needed.

Known limitation: large parallel result batches can now exceed the model window
instead of being automatically shortened. Prefix-cache reuse also depends on
other prompt content; history summaries and reasoning omissions can change it.

- Verification: 1,434 Python/MCP tests passed, the offline tool-result recovery
  example succeeded, wheel and source distributions built successfully, and
  package metadata checks and `git diff --check` passed.
- Three environment-dependent tests were skipped: two opt-in live MiniMax tests
  and one test requiring a running Docker daemon.

## 0.9.17

### Independent skill accessor selection

**Breaking behavior change:** `skill.list` and `skill.view` follow the agent's
capability scope and the catalog's enabled state, independently of `skills`.
An omitted capability scope includes all enabled built-ins, even if no Skills
are configured. An explicit scope includes only its listed tools; an empty scope
exposes none. Selecting a Skill no longer implicitly adds either accessor.

`skills=None`, `skills=[]`, and `skills=[...]` now restrict readable content only.
An empty skill scope keeps selected accessors callable: listing returns no
Skills and viewing rejects access. IDs, function names, capability kinds and
durable SDK schemas remain unchanged.

Migration: add `skill.list` and/or `skill.view` to explicit agent capability
lists that need them, including registered and inline subagents. To hide these
tools, exclude them from the capability list or disable them with
`Runner.set_capability_enabled(...)`; do not use `skills=[]` as a tool switch.

### Scoped agents in the local API and Web UI

Agent presets and inline DAG agent scopes now accept enabled skill accessor
capabilities. Both Web UI editors expose `skill.list` and `skill.view` alongside
other capabilities, independently of selected skill names. Nested agent
capabilities, unknown capabilities, and disabled capabilities remain rejected.

### Release verification and upgrade

Install with `pip install --upgrade dagent-ai==0.9.17`. Apply the explicit accessor
selection guidance above when upgrading from 0.9.16. No request shapes, public
signatures, capability ids, or persisted schema versions change; no stored-record
migration is needed.

Known limitation: skill-routing prompts can still request `skill_view` when that
accessor is omitted. Include `skill.view` in explicit scopes for agents expected
to read skill instructions.

- Verification: 1,426 Python/MCP tests passed, the offline runtime registration
  and skills example succeeded, wheel and source distributions built
  successfully, and package metadata checks and `git diff --check` passed.
- Web verification: 128 frontend tests, the production build, and browser checks
  for selecting and saving accessors in both agent editors passed.
- Three environment-dependent tests were skipped: two opt-in live MiniMax
  tests and one test requiring a running Docker daemon.

## 0.9.16

### ZIP skill package wrappers

- `SkillStore.install()` accepts `SKILL.md` at the archive root or inside a chain
  of enclosing directories, such as `export/terse/SKILL.md`. Installation removes
  these wrappers and preserves all files relative to the skill's `SKILL.md`,
  including linked references, templates, assets, and scripts.
- Each enclosing level must contain only the next directory. Sibling files
  outside the skill, multiple skill directories, and packages missing `SKILL.md`
  are rejected. Unsafe paths, symlinks, duplicate paths, and existing package
  size/file limits remain checked before extraction.
- Wrapper normalization computes the shared directory prefix once and trims each
  path once, removing quadratic work for deeply nested archive paths.

### Skill package directory depth

**Behavior change:** ZIP skill installs now reject archive paths containing more
than 10 directories, counted before wrapper removal. The limit includes both enclosing wrappers and
the skill's internal directories, and applies to explicit directory entries too.
Packages exceeding the limit previously had no explicit depth restriction.
Remove unnecessary wrappers or flatten internal directories, update linked paths
in `SKILL.md`, and repackage before installing. Existing installed skills remain
readable. See [Skills](skills.md) for package limits and examples.

### Release verification and upgrade

Install with `pip install --upgrade dagent-ai==0.9.16`. No public signatures,
request shapes, capability ids, configuration fields, or persisted schema versions
change; no stored-record migration is needed. Apply the archive repackaging
guidance above when an install exceeds the new depth limit.

- Verification: 1,407 Python/MCP tests passed, the offline runtime registration
  and skills example succeeded, wheel and source distributions built
  successfully, and package metadata checks and `git diff --check` passed.
- Three environment-dependent tests were skipped: two opt-in live MiniMax
  tests and one test requiring a running Docker daemon.

## 0.9.15

### Context budgets and result recovery

- After history/reasoning reductions, hard context overflow now reduces tool-result
  displays further, targeting the configured compaction trigger. Per-result and
  total budgets remain ceilings; minimum status, recovery information and short
  excerpts remain. Current input, latest reasoning and tool-call/result pairing
  remain protected. Necessary input that cannot fit still fails before generation.
- Newly shortened originals use existing result storage and warning semantics.
  Structured values, audit records and saved context policies keep their meanings.
- Failed reference-manifest writes retain one storage warning without repeated
  writes during refitting. A recoverable storage failure no longer exhausts the
  context budget by accumulating duplicate warnings.
- SDK-created subagents and auxiliary model roles inherit cached server context
  limits when exact counting becomes unavailable. Unknown-window `auto` fallback
  and strict `vllm` counting retain their existing behavior.
- `context.compaction.finished` now requires actual summarization (model or
  deterministic fallback). Result storage and display shortening alone no longer
  emit a misleading compaction event. Consumers should inspect `ContextUsage`
  tool-result counters for display changes.
- No public signatures, configuration fields or persisted schema versions change;
  no stored-record migration is needed. Update tests that assume display ceilings
  are guaranteed allocations or that result storage emits a compaction event.

### Release verification and upgrade

- Install with `pip install --upgrade dagent-ai==0.9.15`. Hosts should use
  `ContextUsage` to observe result display changes; no API or data migration is
  required when upgrading from 0.9.14.
- Verification: 1,390 Python/MCP tests passed, the offline result-recovery example
  succeeded, wheel and source distributions built successfully, and package
  metadata checks and `git diff --check` passed.
- Three environment-dependent tests were skipped: two opt-in live MiniMax
  tests and one test requiring a running Docker daemon.

## 0.9.14

### Model completion and token diagnostics

- Tool-agent runs now fail explicitly on reasoning-only or empty final model
  turns. `RunResult.error` exposes the typed trace error and stable code;
  `output_text` stays empty. Existing tool results and the last model response
  remain in conversation/trace. Ordinary tool errors still allow the model to
  issue a new valid call. Failure does not automatically rerun tools or the run.
- Built-in providers require terminal protocol information for both streaming
  and non-streaming responses. Missing Chat `finish_reason`, missing Responses
  completion/status, output exhaustion, and explicit failures raise
  `ProviderResponseError` with a partial `response` and `reason`. Invalid tool
  JSON now uses that typed error instead of `ValueError`. ToolAgent converts
  these into failed results and bypasses task validation retries.
- Public `ModelCallAttempt` records and actual termination fields are included
  in `ModelCallMetadata`. Transport/HTTP failures now raise
  `dagent.providers.ProviderRequestError` with `cause`, `metadata`, and any
  partial response. Runner retries transient requests using its existing policy;
  OpenAI client automatic retries are disabled, including for injected clients.
- Routing, result validation, and context compaction use runtime request retries
  too. Step-limit exhaustion still allows validation recovery; only terminal
  model failures bypass validation. No migration is needed for these fixes.
- Invalid `/tokenize` data uses labelled heuristic counting in `auto` and raises
  `ProviderTokenCountError` in explicit `vllm`. Context-window provenance
  distinguishes configured, server, model, and fallback values. A fallback
  131,072-token budget is not evidence of server capacity.
- **Migration:** check `result.status` and `result.error` before showing output;
  return valid termination fields from custom HTTP endpoints/test doubles.
  Catch the typed provider errors rather than raw OpenAI exceptions or invalid
  tool JSON `ValueError`. `ModelTokenUsage` fields are now nullable: handle `None`
  as unknown instead of assuming missing usage is zero. Actual zero stays zero;
  absent totals are no longer inferred. New audit fields are additive; existing
  records remain readable without schema conversion. Strict hosts accepting
  serialized metadata must allow the new fields and nullable usage.
- Regression coverage includes tool-error recovery, inert reasoning call text,
  refusals, partial/unterminated streams, retry diagnostics, and invalid counts.
  See [model contracts](model-context-and-reasoning.md) and the offline
  [failure example](../../examples/model_failure.py).

### Release verification and upgrade

- Install with `pip install --upgrade dagent-ai==0.9.14`. Apply the model-response,
  exception-handling, and nullable token-usage guidance above when upgrading.
- Verification: 1,367 Python/MCP tests passed, wheel and source distributions
  built successfully, package metadata checks and `git diff --check` passed.
- Three environment-dependent tests were skipped: two opt-in live MiniMax
  tests and one test requiring a running Docker daemon.

## 0.9.13

### Mutable uploaded files

- Message and static DAG uploads verify the written size and SHA-256 against
  received bytes immediately. Upload write/verification failures raise `OSError`.
  No client checksum fields or HTTP upload request changes are introduced.
- Historical `Attachment` size and digest are upload-time records. Editing,
  overwriting, or deleting working files no longer fails review or run completion
  through attachment integrity checks.
- **Behavior change:** conversation continuation no longer copies historical
  uploads into a new workspace or rebases their paths. Pass the same
  `workspace_path` for continued editing, or migrate working files explicitly.
  If an existing attachment already points under `.runtime/history`, preserve
  that relative path when migrating its working file. Missing attachments are
  handled by tools when accessed, not restored automatically.
- Original snapshots belong to the host. Existing backing-store objects and
  historical records are not rewritten or deleted. `ContentReference` result
  integrity checks and restoration remain unchanged.
- Public signatures, attachment fields, and conversation/checkpoint schema
  versions are unchanged; no stored-record conversion is required. Hosts matching
  exact model prompt text must update the upload description expectations.
- Regression coverage includes upload corruption, editable/deleted files,
  streaming, review restart, shared-workspace continuation, and result integrity.
  See [workspace guidance](runner-and-configuration.md#editing-uploaded-files)
  and the [runnable example](../../examples/editable_uploads.py).

### Release verification and upgrade

- Install with `pip install --upgrade dagent-ai==0.9.13`. Hosts continuing file
  editing must reuse `workspace_path` or migrate working files explicitly.
- Verification: 1,300 Python/MCP tests passed, wheel and source distributions
  built successfully, package metadata checks and `git diff --check` passed.
- Three environment-dependent tests were skipped: two opt-in live MiniMax
  tests and one test requiring a running Docker daemon.

## 0.9.12

### Batch tool review and editing

- `tool.edit_file` adds optional `replace_all=False`. Explicit `True` replaces
  all non-overlapping exact matches; default unique-match, newline and BOM
  behavior stays unchanged. The capability id is unchanged.
- Shell previews now retain up to 2000 lines instead of 200, still capped at
  100,000 bytes. Capture/storage and model token budgets are unchanged.
- Tool rounds preflight before any execution and wait as a whole when review is
  needed. Unreviewed siblings are saved rather than marked skipped. Rejection or
  an ordinary failure does not stop subsequent calls; required storage failures
  and cancellation still stop execution.
- Existing single-review payloads and `approve/reject` helpers remain supported.
  Hosts must accept the new batch `capability_calls` field and optionally submit
  typed `CapabilityReviewDecision` entries through `capability_decisions`.
  Whole-batch decisions and per-call decisions are mutually exclusive. Rejected
  calls now emit `capability.call.failed` rather than a successful result event.
- **Checkpoint break:** new checkpoints and resolved plans use V9; RunState uses
  V6 with `pending_tool_batch` replacing `pending_invocation`. ConversationState
  remains V4. Complete pending runs before upgrading. V8/older checkpoints are
  rejected without conversion; use the original SDK to finish them or start a
  new run. The edit-tool schema also changes its frozen capability fingerprint.
- Upgrade SDK and API/WebUI consumers together. Historical conversation data is
  not rewritten. Persistence remains JSON-backed; no new database columns are
  required. Cross-process review claiming remains the host's responsibility.
- Verification covers mixed decisions, full-round waiting, restart/cursor
  recovery, boundary rechecks, static Agent nodes, file-byte preservation, shell
  limits, API persistence, WebUI selection and full-argument inspection. Real
  Docker tests depend on a Docker daemon; the feature adds no nested-DAG review
  support or cross-node review aggregation.


### Release verification and upgrade

- Install with `pip install --upgrade dagent-ai==0.9.12`. Complete old pending
  runs using the original SDK before upgrading; old checkpoints cannot resume
  on this release. Upgrade API/WebUI consumers alongside the SDK.
- Verification: 1,279 Python tests passed (3 skipped), 128 web tests passed,
  the production web build and whitespace checks passed. Browser review
  interactions and the batch-review example were also verified.


## 0.9.11

### File windows and display budgets

- File displays distinguish source EOF/window coordinates from the exact
  displayed character interval. Continuation follows the displayed end after
  both per-result and total-budget shortening. Existing retention coordinates
  remain source coordinates; no checkpoint schema or global budget change.
- Line windows now preserve their final LF/CRLF. Raw bounded line reads use
  `[SOURCE_TRUNCATED]` and character continuation; model `[TRUNCATED]` denotes
  display shortening. Human-readable result text intentionally changes; hosts
  parsing it should update expectations and use typed retention for source data.
  Existing saved results are not rewritten.
- Tool descriptions and common prompts recommend cursor-based progress and
  avoiding duplicate intervals. No automatic reading, retries, recovery or
  execution deduplication is added. See [tool result recovery](tool-result-recovery.md).


### Final-request reasoning observability

- `ModelCallMetadata.request_reasoning` is a new optional typed summary of
  final HTTP request reasoning fields, item/character counts and omission reasons.
  Existing `ContextUsage` reasoning counters keep their projection semantics.
- Old records load with `request_reasoning=None` (unknown, not zero). This uses
  the existing optional-field extension policy: Checkpoint V8, RunState V5 and
  ConversationState V4 remain unchanged. No conversion or historical backfill.
- Hosts that strictly validate persisted or streamed nested metadata must accept
  this field or upgrade their SDK/parser before reading new records. New records
  are not guaranteed to load in older SDKs; coordinate consumer upgrades and
  avoid rollback readers on new data. No extra reasoning/body/credential logging.


### DeepSeek replay mapping

- Chat `auto` now sends `reasoning_content` for recognized official DeepSeek V4
  endpoints/models. Explicit field settings, vLLM and Responses retain their
  behavior; unknown endpoints/models remain conservative.
- The active-run boundary and existing budget-driven reasoning omission are
  unchanged. Full protocol compliance under pressure or across Runs is not
  promised. See [reasoning](model-context-and-reasoning.md#deepseek-chat-tool-replay).
- Live acceptance passed on `api.deepseek.com`, `deepseek-v4-flash`, Chat
  Completions with `high` effort: three generation requests, one tool exchange,
  and complete replay of one 95-character reasoning item. Auto and explicit
  `reasoning_content` request messages matched, as did final-request metrics.
  Earlier `low` checks returned empty reasoning and could not validate replay.
  This verifies request carriage and successful responses, not server reasoning
  use or the cause of repeated tool calls. The example now uses `high` effort.


### Stable-release migration and verification

- Install the immutable release with `pip install 'dagent-ai==0.9.11'` and
  update the host dependency lockfile; do not copy SDK source into consumers.
- This stable release also includes the Tool storage-failure audit improvements
  previously shipped in `0.9.10a0`: public `RunExecutionError`, complete preceding
  traces, and optional `RunFailedData.result`. See that entry below for migration.
- Upgrading from 0.9.9 also includes 0.9.10's deliberate `fast` boundary-review
  behavior change. Select `careful` when boundary overrides need human approval.
- Verification: full Python/MCP suite, offline public-SDK examples, wheel/sdist
  builds and package metadata checks; 1,257 tests passed and 3 skipped. Bounded
  DeepSeek acceptance passed as described above. The opt-in MiniMax tests and
  Docker-dependent test remain environment-dependent.

## 0.9.10a0 (0.9.10-alpha)

### Tool result-storage failure audit

- Tool storage-failure snapshots preserve traces from every preceding validation
  attempt, including failures after two or more retries.
- `RunFailedData.result` is a new optional `RunResult` field, defaulting to `None`.
  Stream consumers with strict payload schemas must accept this additional field.
  The terminal event remains `run.failed`, with unchanged message/error type.
- Tool required-storage failures from `run()` / `resume()` now raise public
  `dagent.RunExecutionError`. Replace imports/catches of internal
  `ResultStorageError` with `dagent.RunExecutionError` and audit `exc.result`.
  It remains a `RuntimeError`; the original exception is chained as its cause.
- Failure snapshots preserve execution and retention audit, stop subsequent
  execution, and expose no checkpoint, including after review. DAG failure
  results and recoverable text-storage warnings retain their existing behavior.


### Verification and installation

- Validated with the full Python suite (including MCP), wheel/sdist builds,
  and package metadata checks.
- This is a prerelease. Python normalizes `0.9.10-alpha` to `0.9.10a0`,
  which sorts before the already published stable `0.9.10`. Install explicitly:
  `pip install 'dagent-ai==0.9.10a0'`.
- Audit snapshots do not restore missing data or roll back tool side effects;
  Workspace publication and audit persistence remain host responsibilities.


## 0.9.10

### Fast review automatically approves boundary overrides

- `fast` now means automatic approval of all reviewable operations, including
  tool and DAG path-boundary overrides. It no longer pauses for boundary review.
- This deliberately changes the default authorization behavior. Select `careful`
  if tool-agent boundary overrides must require human approval. Hard-blocked
  shell commands, capability visibility and OS/sandbox permissions are unchanged.

### Verification and known limitations

- Release validation covers the full Python suite and wheel/sdist metadata checks.
- Two real MiniMax integration tests require opt-in; one Docker integration test
  requires a running daemon. These three tests were skipped locally.
- Automatic approval does not enable unavailable capabilities or bypass hard
  command restrictions, operating-system permissions or sandbox isolation.

## 0.9.9

### Recoverable tool output

- Fix aggregate file-read observations inheriting a child's window metadata and
  complete file results being rejected when shortening adds recovery notices.
- The default total tool-result display budget increases from 8192 to 16384
  tokens; the individual limit remains 2048. Set the total explicitly to retain
  8192. Existing checkpoint policies remain explicit and unchanged.
- Results now reserve status, excerpts and recovery notices before distributing
  remaining tokens. Display truncation can save text below the 256 KiB inline
  threshold. Enable `tool.read_file` and allow the result directory when needed;
  capabilities and boundaries are never automatically expanded.
- Shell capture uses bounded memory with a configurable shared 64 MiB limit;
  commands continue above the capture limit with explicitly partial records.
  `read_file` adds character windows; `grep` and `list_files` add entry pagination.
- Result retention, content references, planner observations and summary indexes
  add optional schema fields. New SDK versions read existing v4 checkpoints;
  older SDKs are not guaranteed to accept new checkpoint fields. Internal built-in
  handlers now return `ToolOutput`; public capability IDs and structured value
  shapes are unchanged. Visible result/observation text intentionally changes.
- Text-storage failures warn without changing execution success. Required value
  or artifact failures block dependent execution. Saving originals can increase
  disk use and persist sensitive output; workspace lifecycle belongs to the host.
  See [Tool Result Recovery](tool-result-recovery.md) and its offline example.

### Context window defaults and DeepSeek model recognition

- When no explicit or discovered limit is available, the SDK now uses 131,072
  tokens (128K), up from 32,768. To preserve the previous limit, set
  `context_window_tokens=32768` explicitly. Saved checkpoint limits remain unchanged.
- Official DeepSeek V4 Flash, Pro, and Flash Vision Exp model IDs are recognized
  with a 1,048,576-token window from official documentation checked
  on 2026-09-07. `/models` does not expose lengths; this is an endpoint/model lookup.
  Third-party endpoints and unknown models do not inherit these limits.
- Explicit limits remain authoritative but cannot exceed a known model maximum.
  DeepSeek auto counting uses the heuristic; explicit vLLM counting fails.
  Context usage adds optional `model_context_window_tokens` for the documented
  model limit. Token estimation remains independent of model-limit recognition.

### Verification and known limitations

- Release validation covers the full Python suite and wheel/sdist metadata.
- Real MiniMax integration tests require explicit opt-in; Docker integration
  requires a running daemon. These three tests were skipped locally.
- Recovery requires the saved workspace and permitted file tools. Remote MCP
  data discarded before reaching the SDK cannot be recovered. Hosts own disk
  quotas, retention and cleanup; the shell limit is per invocation.

## 0.9.8

### Breaking: flat provider reasoning controls

- Replace `reasoning={"effort": "high", "capture": "field_and_tags"}` with
  `reasoning_effort="high"` and `reasoning_capture="field_and_tags"` in Python,
  or the equivalent flat keys in YAML. The old nested `reasoning` object is
  rejected; no compatibility alias is provided.
- `reasoning_effort` controls model generation and maps to Chat
  `reasoning_effort` or Responses `reasoning.effort`. `reasoning_capture`
  controls response parsing only and defaults to `field_and_tags`.
- `ContextPolicy.reasoning_replay` is unchanged and remains the independent
  context-assembly policy for replaying stored reasoning.

### Verification and known limitations

- Release validation covers the full Python suite, WebUI tests and production
  build, and wheel/sdist metadata checks.
- Effort-level support remains model-dependent. A private vLLM endpoint must
  expose the selected reasoning capability for `reasoning_effort` to take
  effect.

## 0.9.7

### Breaking: unified private-vLLM model context

- The built-in `Provider` now supports private vLLM Chat Completions and
  Responses through one internal request/response contract. All SDK model call
  sites use it. Existing custom `ChatProvider` implementations remain supported
  by an explicit Chat adapter; the public `Provider` and
  `OpenAICompatibleProvider` class names are unchanged.
- `Provider.protocol` is `auto`, `chat_completions`, or `responses`. Auto mode
  probes `/openapi.json` and `/version`, prefers Responses when capabilities are
  equivalent, and falls back to Chat with a warning only when discovery is
  inconclusive. Explicit protocols never cross-fallback, including after a
  failed POST.
- Responses calls are stateless: `store=False`, no `previous_response_id`, no
  encrypted reasoning content, and no persisted provider item IDs.
- `ReasoningConfig.enabled` was removed. Delete it from Python/YAML config.
  `effort` is now typed as `none|minimal|low|medium|high|xhigh|max`.
  `budget_tokens` was also removed and is now rejected; use only `effort`.
- `ContextPolicy.keep_recent_turns` was removed. Replace it with
  `reasoning_replay="none|active_run|all_runs"`; the default is `active_run`.
  Compaction is token-driven and preserves the current input, open tool chain,
  and latest step.
- `token_counting="auto|vllm|heuristic"` controls exact vLLM `/tokenize`
  accounting. `context_window_tokens=None` uses discovered `max_model_len`; an
  explicit value overrides it but cannot exceed it. The no-discovery fallback
  remains 32,768.
- `output_reserve_tokens` was removed. Use optional `max_output_tokens`, which
  both reduces input capacity and is sent as the real generation limit. This is
  not a one-to-one rename; leaving it unset sends no output limit.
- Context compaction now triggers at 80% of usable input capacity, targets 16%
  retained recent history, emits summaries up to 8,192 tokens, and uses the
  independent `compaction_reasoning_effort="low"` by default.
- New checkpoints use schema V8, RunState V5, and ConversationState V4. Older
  checkpoints are rejected; there is no conversion or compatibility shim.
  Finish pending reviews before upgrading.

See [Model Context and Reasoning](model-context-and-reasoning.md) for wire
examples and the complete selection/replay policy.

### Verification and known limitations

- Release validation covers the full Python suite, WebUI tests and production
  build, and wheel/sdist metadata checks.
- Automatic capability and context-window discovery depends on private vLLM
  exposing `/openapi.json`, `/version`, and `/tokenize`. When discovery is
  unavailable, the documented warnings, protocol choice, token estimator, and
  32,768-token context fallback apply.

## 0.9.6

### Breaking: one agent-owned execution bound

- `ToolAgent`, `AutoAgent`, and `DagAgent` now expose the same single
  `max_steps` field with a literal default of `888`. `ToolAgent` counts local
  tool-loop iterations, `DagAgent` counts dynamic DAG cycles, and `AutoAgent`
  applies the value to whichever engine its router selects. Provider retries,
  validation retries, and context compaction do not consume agent steps.
- Replace `DagAgent(max_cycles=n)` with `DagAgent(max_steps=n)`. Remove
  `AutoAgent.max_cycles`; its existing `max_steps` now also bounds a dynamic DAG
  route. This is an intentional public API break with no alias.
- `Runner.run(...)` and `Runner.stream(...)` no longer accept `limits=`.
  `ExecutionLimits` and `ExecutionLimitExceeded` are no longer public exports.
  Remove those imports and move local execution bounds to the target agent.
  Static `Dag`/`DAGSpec` runs have no root-wide operation limit; Map and Loop
  structural bounds are unchanged.
- An `agent.*` capability accepts `prompt` and `reference_content`, but no
  invocation-level `max_steps`. Configure the registered `ToolAgent.max_steps`
  instead. Older persisted DAG node arguments remain readable and executable;
  an old `max_steps` argument is ignored with `DeprecationWarning`.
- `ExecutionUsage` remains cumulative telemetry for model turns and capability
  calls. It is restored across review resume and starts from zero for a new
  conversation run, but no longer enforces a budget.
- New checkpoints use schema V6 and store one `max_steps` value for agent runs.
  Static DAG plans store no root step value. V4/V5 checkpoints remain readable
  and resumable: their global limits are ignored with `DeprecationWarning`,
  their tool `max_tool_steps` or dynamic-DAG `max_dag_cycles` becomes the local
  effective bound, and a successful resume emits V6.
- A V4/V5 checkpoint whose registered child agent relied on the former
  `ToolAgent.max_steps=8` default must keep that child explicitly pinned to
  `max_steps=8` for every remaining resume of that run. Registering the same
  constructor without the explicit value now means `888`; Runner reports this
  case with a targeted migration error instead of silently changing execution
  semantics. New runs can use the `888` default.
- The bundled API and WebUI keep `max_steps` on reusable child-agent presets;
  the default for newly created presets is now `888`. Agent-node capability
  forms no longer offer a per-invocation step override.

### Added: cooperative steering for active root tool-agent runs

- `await Runner.steer(run_id, input)` queues text guidance for an active root
  `ToolAgent` loop and returns `SteerReceipt`. The same behavior is available
  while `run`, `stream`, `resume`, or `resume_stream` is executing.
- `AutoAgent` supports steering only after routing resolves to `tool`. Static
  and dynamic DAG runs reject steering, as do validation and awaiting-review
  phases. Review feedback remains the continuation input at a review gate.
- Typed streams add `steer.queued`, `steer.applied`, and `steer.discarded`.
  Queues are FIFO and bounded at 32 messages. In-flight model and capability
  calls are never interrupted; unstarted sibling tool calls are skipped.
- Steering itself adds no fields to `RunState` or `RunCheckpoint`. Hosts that
  choose to expose it must retain the same in-process `Runner`; queued messages
  are intentionally ephemeral and are not checkpointed. The separate V6
  checkpoint migration in this Unreleased section still applies.

## 0.9.5

### Changed: observable DAG design provider streams

- `Runner.design_dag(...)` accepts an optional synchronous `on_event` callback.
  When present, it consumes the provider's real stream and reports existing
  `RunStreamEvent` response and validation events. Structured candidate JSON is
  not exposed as content deltas, and the awaited `DAGDesignResult` remains the
  only terminal value. Calls without a callback retain the 0.9.4
  `provider.chat` transport behavior.
- When `agent` is omitted, design now uses the dedicated built-in `dag_design`
  profile instead of the execution-oriented `dag_agent` profile. Pass
  `DagAgent(planner_profile="dag_agent")` to retain the previous prompt, or an
  explicit custom profile for host-specific instructions.
- Returned design conversations now store the natural summary, answer, or
  deterministic failure message as visible assistant content instead of the
  provider's raw structured JSON. No schema or persistence migration is
  required; existing conversation values remain valid inputs.
- The design-only `dag_design` profile remains loadable by
  `Runner.design_dag(...)`, but the bundled host does not advertise or accept it
  as an executable `agent.dag_design` capability.

## 0.9.4

### Added: neutral, non-executing DAG design

- `Runner.design_dag(...)` now creates, revises, checks, or explains a complete
  typed `DAGSpec` candidate from natural language. Its tagged result variants
  are `DAGDesignProposal`, `DAGDesignNoChange`, `DAGDesignAnswer`, and
  `DAGDesignFailure`.
- `DAGDesignSelection` carries optional selected node ids. Every result carries
  a continued `ConversationState`, provider `ModelTokenUsage` when available,
  request `ContextUsage`, and structured `DAGDiagnostic` values.
- `inspect_dag_spec(...)` exposes deterministic diagnostics without changing
  the existing `validate_dag_spec(...)` exception contract.

### Behavior and compatibility

- Design calls use the normal Runner catalog and optional `DagAgent` scope.
  Candidate capability kind, risk, and boundary are always replaced with
  catalog values; unknown, disabled, or out-of-scope ids fail closed.
- A design call invokes only the chat provider. It creates no Run, review,
  checkpoint, workspace artifact, or capability result, and never invokes a
  capability handler. Existing `Runner.run(...)` behavior is unchanged.
- Revisions return a full candidate, preserve stable retained node and
  invocation ids where semantics are unchanged, and retain unchanged edge
  fields and ordering. `DAGEdge` gains no id or layout field.
- No data or configuration migration is required. Hosts continue to own
  persistence, revisions, semantic diff, partial acceptance, layout, policy,
  and audit.

### Changed: approved boundary paths are reusable within one run

- A reviewable boundary violation now includes normalized
  `payload.boundary_paths`. Approval authorizes only those reported paths for
  later tool-agent calls in the same run, so a new invocation id does not cause
  a duplicate review for the same path.
- Different paths still require review. The authorization is not persisted as
  user, project, or cross-run policy, and hard boundary failures remain blocked.
- `RunState` and `RunCheckpoint` schema versions are unchanged. New checkpoints
  carry the authorization through the existing invocation boundary. Older
  checkpoints without `boundary_paths` retain one-invocation approval behavior.

### Changed: project-neutral static orchestration runs

- The bundled API/WebUI now treats a saved static DAG as a reusable definition,
  not a project or conversation resource. Each call to
  `POST /saved-dags/{dag_id}/run/stream` creates a new run id and an isolated
  `.dagent/projects/_runs/<run_id>/workspace`.
- New static runs do not acquire a conversation lock. Their run rows have null
  `project_id` and `conversation_id`, and history remains grouped by
  `saved_dag_id`.
- Archiving a saved DAG deletes its grouped run history and isolated run
  workspaces; active definitions return `409` until queued/running runs finish.
- Static reviews resume through `POST /reviews/{review_id}/resume` using the
  persisted `RunCheckpoint` and run-owned workspace. This remains deterministic
  across an API or `Runner` restart.

### Local API and storage compatibility

- `POST /saved-dags` no longer accepts `project_id`, and saved-DAG responses no
  longer contain it. `GET /saved-dags` no longer filters by project.
- `POST /saved-dags/{dag_id}/run/stream` no longer accepts `project_id` or
  `conversation_id`; send only optional `graph_input`.
- SQLite migration version 2 detaches existing saved DAG definitions by setting
  a legacy `saved_dags.project_id` column to null. New databases do not create
  that column. Deleting a project no longer deletes saved DAG definitions or
  their uploaded inputs.
- Existing conversation-backed static runs and pending reviews remain readable
  and resumable through their original conversation/project route. This
  compatibility path is only for already persisted runs; it is not used for new
  static runs.
- These are changes to the bundled local API/WebUI contract. The public Python
  SDK, `DAGSpec`, `RunState`, and `RunCheckpoint` schemas and versions are
  unchanged by this change.

### Migration steps

- Remove `project_id` from saved-DAG create calls.
- Remove `project_id` and `conversation_id` from saved-DAG run calls. Group run
  history with `GET /saved-dags/{dag_id}/runs` instead of a conversation.
- Resume a newly created static run's review with the project-neutral review
  endpoint. Do not create a static conversation or orchestration session for a
  new run.

## 0.9.3

### Added

- Static DAG input artifacts now expose `artifact.files`: a deterministic,
  JSON-safe list of the `ArtifactFileRef` values materialized from this run's
  `artifact_uploads`. Entries carry a workspace-relative `path`, `name`,
  `size`, and optional caller-supplied `media_type`.
- `ArtifactFileRef` and `ArtifactFileManifest` are public SDK contracts.
  `MapNode(over=artifact.files, ...)` can process the current `dagent.item`
  file entry directly.

### Behavior and compatibility

- The list is an upload-time snapshot, not a workspace scan. Empty optional
  inputs resolve to `[]`; later files and files from other runs cannot appear
  during checkpoint resume.
- Materialization rejects unsafe names, traversal, duplicate targets,
  symlinked upload paths, more than 256 files, a file over 25 MiB, or more than
  100 MiB in total. Existing `artifact.path`, `artifact.paths`, and absolute
  artifact expressions are unchanged.

### Checkpoint versions and compatibility

- New runs emit `RunState` V4 and `ResolvedRunPlan` / `RunCheckpoint` V5 so a
  checkpoint persists the authoritative input-file snapshot.
- Existing V4 checkpoints containing a V3 `RunState` remain restorable. They
  explicitly have an empty input-file manifest: resume never scans the
  workspace or derives files from it. A successful resumed result emits a new
  V5 checkpoint.
- Other version combinations remain invalid. In particular, V3 state cannot
  claim to contain an input-file manifest, and the SDK does not transparently
  manufacture one for an older checkpoint.

### Migration steps

- Hosts may continue restoring persisted V4 checkpoints. Persist the V5
  checkpoint returned after a successful resume, and use V5 for all new runs.
- For a file input, declare a directory artifact (for example
  `inputs/workflow_input_files/`) and call `Runner.run(...,
  artifact_uploads={artifact_id: [ArtifactUpload(...)]})`. Consume
  `artifact.files`, a selected entry such as `artifact.files[0].path`, or map
  over the list. Do not scan the run workspace to recreate this contract.

### Verification and known limitations

- `uv run --extra dev pytest tests/test_artifact_file_manifests.py`
- `uv run --extra dev pytest`
- `artifact.files` is available for static `Dag` and `DAGSpec` runs, where
  `artifact_uploads` is accepted. It does not add a workflow-start protocol or
  alter `StartNodePayload`.

## 0.9.2

### Added

- A top-level, direct `Node(..., target=ToolAgent(...))` in a static DAG now
  uses the existing `ToolAgent` review and resume flow for its inner tool calls.
  With `review="careful"`, medium- and high-risk inner tools pause for review;
  under every policy, a boundary violation can pause for a one-invocation
  boundary override.
- Static-DAG agent review checkpoints persist the suspended node invocation and
  inner tool-agent state. They can be restored by a compatible new `Runner`.
  The direct agent configuration is fingerprinted so a changed profile, limits,
  policy, skill scope, or inner-tool scope cannot silently alter a resumed run.
- The WebUI now presents these static-DAG reviews, keeps the run in
  `awaiting_review` until a decision is made, and can resume a saved static DAG
  without a separate conversation-state record.
- The `dev` extra now includes `pip`, so `uv sync --extra dev` and
  `uv run --extra dev` provide `python -m pip` for development workflows.

### Behavior and compatibility

- Ordinary static capability nodes, including high-risk nodes, remain directly
  authorized by the DAG author and do not gain a review prompt.
- Only direct top-level agent capability nodes are supported. Agents in a
  `MapNode`, `Subgraph`, or `LoopNode`, and registered agents that expose another
  agent, are rejected before execution because their nested progress cannot yet
  be restored safely.
- Approval and rejection continue the same inner `ToolAgent` conversation;
  rejection does not execute the reviewed tool and is returned to the model as
  feedback.

### Breaking changes

- None.

### Migration steps

- Upgrade `dagent-ai` from `0.9.1` to `0.9.2`. No data or configuration
  migration is required.
- Contributors who need to run `pip` in the project environment should refresh
  it with `uv sync --extra dev`.

### Verification and known limitations

- `uv run --extra dev --extra mcp --frozen pytest`
- `npm --prefix web test`
- `npm --prefix web run build`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`
- Static agent review deliberately does not support `MapNode`, `Subgraph`, or
  `LoopNode` continuations yet, and it does not change dynamic-DAG semantics.

## 0.9.1

### Changed

- Tool-loop system prompts now include deterministic routing metadata for the
  resolved skill scope. Explicitly requested or clearly matching skills are
  loaded through `skill.view`; full `SKILL.md` bodies remain on demand.
- Complete name-and-description entries use an 8,000-character budget. Later
  name-only entries use a separate 2,000-character budget, followed by an
  omitted count and `skill.list` fallback when necessary.
- Skill routing entries use compact JSON arrays: `[qualified_name, description]`
  for complete entries and `[qualified_name]` for name-only fallbacks.

### Behavior and compatibility

- `skills=None`, `skills=[]`, and explicit skill filters retain their released
  visibility semantics. No request, checkpoint, configuration, or capability-id
  shape changed.
- Stable qualified-name ordering keeps the system prompt identical across
  conversation turns while the selected scope and skill metadata are unchanged.
- The index applies to tool loops and skill-bound subagents. Dynamic DAG planner
  prompts remain unchanged.

### Breaking changes

- None.

### Migration steps

- Upgrade `dagent-ai` from `0.9.0` to `0.9.1`. No data or configuration
  migration is required.
- Hosts that snapshot exact tool-loop system messages should update those
  snapshots to include the deterministic `Available Skills` block whenever the
  resolved skill scope is non-empty.

### Verification and known limitations

- `uv run --extra dev --extra mcp --frozen pytest`
- `uv run --extra dev --frozen ruff check dagent api tests`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`
- Dynamic DAG planner prompts deliberately do not receive the business-skill
  index. Skill-bound tool subagents receive their own resolved index when run.

## 0.9.0

### Added

- Static and dynamically planned DAGs can use the first-class `ConditionNode`
  and `Case` builder contracts for ordered, exclusive IF/ELIF/ELSE routing.
- `DAGEdge.branch` connects a declared condition branch to downstream nodes.
  One selected branch may fan out to multiple targets; an unconnected selected
  branch ends that path normally.
- `all_of`, `any_of`, and `not_` add structured, short-circuiting boolean value
  expressions without introducing script evaluation.
- Condition execution records `{"branch": ...}` as the node value and
  `selected_branch` on the trace node.
- The typed planner, builder-source planner, local FastAPI saved-DAG contract,
  and WebUI static/dynamic editors understand condition nodes and branch edges.

### Changed

- `when` remains an ordinary edge gate for simple independent dependencies.
  It is not used to encode exclusive branch selection.
- Outgoing edges from a condition node must declare one of that node's branches.
  `branch` is invalid on other sources, and one edge cannot declare both
  `branch` and `when`.
- The local API continues accepting the released capability-node request shape
  without `type`; newly serialized saved DAGs write the canonical
  `type: "capability"` discriminator.

### Behavior and compatibility

- Existing `Node`, `MapNode`, `LoopNode`, and `add_edge(..., when=...)`
  workflows retain their behavior.
- Condition cases are checked in declaration order. Exactly one branch id is
  selected, with the required `default_branch` used when no case matches.
- Node-output references inside cases still require explicit upstream edges.
  Branch declarations do not infer dependencies.

### Breaking changes

- No existing Python builder call is removed. Hosts with exhaustive JSON union
  decoders must accept `payload.type == "condition"`, optional
  `DAGEdge.branch`, and optional trace-node `selected_branch` before consuming
  DAGs produced by 0.9.0.

### Migration steps

- Upgrade `dagent-ai` from `0.8.x` to `0.9.0`.
- Enterprise hosts using an upper bound below 0.9 must update it, for example to
  `dagent-ai>=0.9.0,<0.10.0`.
- Update workflow UIs to create branch edges from a condition node's declared
  handles. Keep `when` available only as the existing ordinary-edge gate.

### Verification and known limitations

- `uv run --extra dev pytest`
- `npm --prefix web test`
- `npm --prefix web run build`
- `git diff --check`
- Condition nodes deliberately provide structured expressions, not arbitrary
  scripts.
- This release does not introduce separate LLM, HTTP request, or human-input
  node contracts.

## 0.8.7

### Added

- The local FastAPI backend's `UserDAG` request contract now accepts a JSON
  `output` value or expression and passes it through the public `Dag.output`
  builder contract. Direct and saved static DAG runs can therefore return the
  resolved structure through `RunResult.output_value`.
- Saved DAG create, read, and update flows preserve the declared output in the
  existing `spec_json` document.

### Changed

- `Runner(...)` and `Runner.from_config(...)` now default `workspace` to
  `Path.home() / ".dagent"` and `runtime_directory` to `.runtime`.
- Explicit values keep their existing behavior. `runtime_directory` remains a
  safe relative path inside the runner or run workspace.

### Behavior and compatibility

- Existing saved DAG documents without `output` load with `output=None`; no API
  database migration or stored-spec conversion is required.
- Existing hosts that pass runtime paths explicitly retain their exact storage
  layout. The local FastAPI backend continues to pass its paths explicitly.
- `output_text` and non-static result behavior are unchanged. Persisted
  `run.finished` events contain `output_value`; run summaries continue to expose
  `output_text` only.

### Breaking changes

- None.

### Migration steps

- Upgrade `dagent-ai` from `0.8.6` to `0.8.7`. No data or configuration
  migration is required.
- Applications that want SDK-managed local storage may omit either or both
  runtime-path arguments.

### Verification and known limitations

- `uv run --extra dev --extra mcp --frozen pytest`
- `uv run --extra dev --frozen ruff check dagent api tests`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`
- The local WebUI does not yet provide an editor for declaring static DAG
  output expressions, and run summaries do not project `output_value` directly.

## 0.8.6

### Fixed

- Pydantic graph inputs are dumped with field aliases when checked against the
  alias-based schema generated by Pydantic. Valid aliased model instances are
  no longer rejected before a static run.
- Resolved subgraph inputs and every loop-body iteration now validate against
  the embedded `DAGSpec.input_schema` before executing a child capability.
- `RunStreamEvent.model_validate(...)` now selects the exact data model from
  the event envelope type. Events with identical payload shapes, such as
  `response.started`/`response.finished` and completed/failed capability calls,
  round-trip to the correct public type.

### Behavior and compatibility

- Embedded input failures use the existing `DAGInputValidationError` contract
  and fail the owning static DAG node without calling child capabilities.
- Input validation still does not mutate values or apply JSON Schema defaults.
  `RunResult`, event envelope, checkpoint, and review behavior are unchanged.

### Breaking changes

- None.

### Migration steps

- Upgrade `dagent-ai` from `0.8.5` to `0.8.6`. No data or configuration
  migration is required.

### Verification and known limitations

- `uv run --extra dev --extra mcp --frozen pytest`
- `uv run --extra dev --frozen ruff check dagent tests`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`

## 0.8.5

### Added

- `validate_dag_spec(...)` now checks every `DAGSpec.input_schema` as a valid,
  self-contained JSON Schema Draft 2020-12 document.
- `validate_dag_input(spec_or_schema, graph_input)` validates an instance before
  static execution. Instance failures raise the public
  `DAGInputValidationError`, including the instance and schema paths.
- `RunResult.output_value` exposes the exact resolved static `DAGSpec.output`,
  including scalar, list, and object values. Serialized results and
  `run.finished` events include the field.

### Behavior and compatibility

- Invalid static graph input is rejected before workspace creation, capability
  execution, or `run.started` emission. Validation does not mutate input or
  apply JSON Schema defaults.
- SDK schemas are not restricted to top-level objects or one spelling of local
  references. Any Draft 2020-12 schema whose referenced resources are embedded
  in the same document remains valid.
- `output_text` formatting is unchanged. Non-static runs set `output_value` to
  `None`; static checkpoints continue to carry the resolved value in the run
  trace.

### Breaking changes

- None.

### Migration steps

- Upgrade the `dagent-ai` dependency to `0.8.5`.
- Consumers that need structured static output can read `result.output_value`;
  existing consumers can continue reading `result.output_text` unchanged.

### Verification and known limitations

- `uv run --extra dev pytest`
- `git diff --check`
- JSON Schema references must resolve from resources embedded in the schema;
  the SDK does not fetch external schemas during validation.

## 0.8.4

### Fixed

- The built-in `conversation` profile no longer describes `dag_agent` or
  includes DAG-orchestration instructions. It now contains only general direct
  response and tool-selection guidance for bounded tool loops.

### Behavior and compatibility

- `AutoAgent` still defaults to `profile="conversation"` and
  `planner_profile="dag_agent"`. Its separate router prompt still chooses the
  tool or DAG path; `ToolAgent` and `DagAgent` execution semantics are unchanged.
- This is a prompt-only SDK fix with no API, schema, capability-id, or
  configuration-shape changes.
- Existing V4 checkpoints remain restorable and retain their frozen original
  profile. Start a new run after upgrading to use the revised built-in profile.

### Breaking changes

- None.

### Migration steps

- Upgrade the `dagent-ai` dependency to `0.8.4`. No data conversion or host-side
  configuration change is required.
- Downstream hosts should consume the published SDK profile instead of copying a
  private replacement.

### Verification and known limitations

- `uv run --extra dev --extra mcp --frozen pytest`
- `uv run --extra dev --frozen ruff check dagent tests`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`
- The change applies only to the built-in `conversation` profile. Custom
  profiles and checkpoints with frozen profiles are intentionally unchanged.

## 0.8.3

### Added

- `Runner(extra_system_prompt=...)` adds one runner-wide literal instruction
  after the Agent Profile and Runtime Context, and before dynamic tool,
  capability-catalog, and DAG-schema content.
- The prompt applies to `ToolAgent`, AutoAgent's selected Tool or DAG path,
  initial DAG planning and replanning, and registered agents.
- `ResolvedRunPlan` and `RunCheckpoint` freeze the run's initial prompt so
  review continuation is independent of later Runner configuration changes.

### Behavior, compatibility, and migration

- `None` preserves the previous model prompt. Existing V4 checkpoints whose
  resolved plans predate this optional field retain their fingerprints and
  remain restorable.
- Configured prompts must be non-blank strings of at most 16,384 characters.
  Values are inserted literally without Jinja, targets, or template expansion.
- The prompt affects model instructions only. It does not alter capability
  visibility, boundaries, review requirements, or workspace permissions.
- `ValidatorAgent`, `FeedbackLearnerAgent`, and AutoAgent's routing classifier
  do not receive the additional prompt.
- This patch release has no breaking API or schema changes and requires no
  migration.

### Verification and known limitations

- `uv run --extra dev --extra mcp --frozen pytest`
- `uv run --extra dev ruff check dagent tests/test_agent_sdk_public_api.py tests/test_prompt_builder.py tests/test_run_checkpoint_budget.py`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`
- Static DAG execution has no planner prompt; registered-agent nodes in a static
  DAG still receive the Runner prompt.

## 0.8.2

### Fixed

- Dynamic DAG review continuation now advances `ConversationState.revision`
  when execution failure and replanning produce another review gate, even when
  the visible conversation items are unchanged. `RunResult`, `RunState`, and
  `RunCheckpoint` expose the same replacement conversation snapshot.
- Planner model-thread revisions now advance from the prior state version
  instead of being derived from the number of items.

### Compatibility and migration

- This patch release has no breaking API or schema changes.
- Hosts should retain strict revision compare-and-swap and duplicate/stale
  rejection. Do not add an equality fallback for 0.8.1.
- Existing V4 review checkpoints and V3 conversations can resume on 0.8.2
  without conversion.

### Verification

- `uv run --extra dev --extra mcp --frozen pytest`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`

## 0.8.1

### Breaking changes

- `Runner(...)` and `Runner.from_config(...)` now require explicit `workspace`
  and `runtime_directory` keyword arguments. The SDK no longer chooses or
  creates a cwd-relative `.dagent` workspace implicitly.
- `runtime_directory` is a safe relative path inside the runner or run
  workspace. Absolute paths, drive-qualified paths, empty paths, `.` and `..`
  segments are rejected.
- `ResultStoragePolicy.internal_directory` was removed. The policy now controls
  only `max_inline_bytes`; all SDK-private paths belong to the runner.
- `ResolvedRunPlan` and `RunCheckpoint` use schema version 4 and freeze
  `runtime_directory`. V3 checkpoints are rejected without conversion.
  `ConversationState`, `ContentReference`, and `RunState` remain schema version
  3.

### Storage layout and model context

- Conversation backing resources use
  `<workspace>/<runtime_directory>/conversations`.
- Externalized tool and MCP results use
  `<run-workspace>/<runtime_directory>/results`; resources restored into a new
  run use `<run-workspace>/<runtime_directory>/history`.
- These private directories are created lazily. Runner construction, text-only
  turns, and small inline results do not create them.
- A continuation first verifies and reuses a resource already reachable at its
  workspace-relative path. It materializes from the conversation backing store
  only when that path is missing.
- Model input includes bounded, deduplicated references for externalized
  content, values, and MCP artifacts. References include a complete relative
  path, media type, byte count, and digest and count against tool-result token
  budgets. Absolute paths and conversation backing-store paths are not exposed.

### Migration

Choose both locations in the host and pass them on every runner:

```python
runner = dagent.Runner(
    workspace="agent-workspace",
    runtime_directory=".runtime",
    provider=provider,
)

runner = dagent.Runner.from_config(
    "config.yaml",
    workspace="agent-workspace",
    runtime_directory=".runtime",
)
```

Move a former result-directory choice from `ResultStoragePolicy` to the runner:

```python
# 0.8.0
policy = dagent.ResultStoragePolicy(
    max_inline_bytes=64 * 1024,
    internal_directory=".dagent/results",
)

# 0.8.1
runner = dagent.Runner(
    workspace="agent-workspace",
    runtime_directory=".dagent",
    provider=provider,
    result_storage_policy=dagent.ResultStoragePolicy(
        max_inline_bytes=64 * 1024,
    ),
)
```

To reproduce the old cwd layout exactly, use
`workspace=".dagent", runtime_directory=".dagent"`. Directory moves are
host-owned; the SDK does not migrate files automatically. Complete, deny, or
cancel pending V3 reviews before upgrading. V3 conversations can continue when
their referenced files or runner conversation backing resources remain
reachable.

If private runtime data should live in memory, mount the chosen workspace or
runtime subtree on tmpfs. The SDK deliberately does not add a second in-memory
storage implementation.

### Verification

- `uv run --extra dev pytest`
- `uv build`
- `git diff --check`

## 0.8.0

### Fixed

- The bundled API and WebUI now use the 0.8 `input`/`ConversationState`
  contract and persist complete review checkpoints with atomic resume claims.
- Attachments and externalized tool/MCP results retained by a conversation
  remain reachable when the next turn uses a fresh run workspace.
- Compactor output now obeys its token budget, reviewed tool failures retain
  failed status, and only typed externalization provenance is interpreted as a
  `ContentReference`.
- SQLite upgrades mark pre-V3 conversations explicitly and reject them with
  HTTP 409 instead of attempting to parse old run state.
- OpenAI-compatible streaming usage metadata is opt-in, so endpoints that
  reject `stream_options` continue to work.
- API/Web model records preserve per-model context windows and output reserves,
  and review checkpoints retain their frozen limits across repeated gates.
- Static DAG map outputs stay externalized in parent traces, and DAG traces
  retain typed references for normalized values and diagnostic fields.
- Capability-handler exceptions are recorded as failed tool results and failed
  trace nodes.

### Breaking changes

- Agent runs now accept `input="..."` plus an optional typed
  `conversation=ConversationState`. The raw `messages=` parameter was removed.
- Ordinary cross-run continuation no longer accepts `state=` or `checkpoint=`.
  Pass `result.conversation` to the next independent run.
- Review continuation now requires
  `resume(decision, checkpoint=result.checkpoint)`. State-only resume and
  in-memory fallback were removed.
- `RunState`, `RunCheckpoint`, and `ResolvedRunPlan` use schema version 3.
  Version 1/2 payloads are rejected; there is no runtime conversion layer.
- `RunResult.messages`, `RunState.internal_messages`, and
  `RunState.input_message_count` were removed.
- `Provider.strip_thinking` was removed. Reasoning capture is controlled by
  `reasoning.capture` and is represented separately from visible content.
- Invalid JSON or non-object tool-call arguments from OpenAI-compatible
  providers now fail explicitly instead of becoming an empty argument object.

### Added

- Provider-neutral `ConversationState`, `UserMessage`, `AssistantMessage`,
  `ToolResultMessage`, `ToolCallItem`, `Attachment`, `ContextSummary`, and
  `ContentReference` contracts.
- One context assembler for tool agents, DAG planners, routers, validators, and
  registered subagents. It budgets the system prompt, schemas, history,
  summaries, and tool results before every model call.
- Model compaction with an explicit deterministic fallback, per-tool and
  aggregate tool-result limits, preflight `ContextWindowExceeded`, typed
  context-usage records, and compaction stream events.
- Reasoning and provider token usage on assistant audit items. Reasoning is
  never replayed into later model input.
- Run-workspace externalization for large text and binary tool/MCP results,
  with SHA-256 references and atomic writes.
- Review checkpoints freeze and verify capability-definition fingerprints
  before executing an approved tool or MCP call.

### Migration

```python
# 0.7
messages = [{"role": "user", "content": "Remember blue."}]
first = await runner.run(agent, messages=messages)
messages += first.messages
messages.append({"role": "user", "content": "What color?"})
second = await runner.run(agent, messages=messages, state=first.state)

# 0.8
first = await runner.run(agent, input="Remember blue.")
second = await runner.run(
    agent,
    input="What color?",
    conversation=first.conversation,
)
```

Hosts must persist the complete V3 checkpoint for pending reviews and the
complete bounded `ConversationState` for chat continuation. See
[Host migration for 0.8](host-migration-0.8.md).

## 0.7.6

### Added

- Profile-backed model calls receive a dynamic `Runtime Context` system-prompt
  section containing the resolved workspace root and guidance to resolve
  relative file paths from that root. `Runner` supplies the run workspace to
  tool agents, dynamic DAG planners, registered DAG subagents, and result
  validators without changing profile Markdown.
- Lower-level `ProfiledAgent`, `ValidatorAgent`, and `FeedbackLearnerAgent`
  calls accept an optional `workspace_path` for the same prompt context.

### Changed

- Registered DAG subagents now use the shared prompt-builder workspace context
  instead of maintaining a separate workspace line in their DAG context.
  Profile contents remain immutable and are not treated as templates.

### Migration

- No migration action is required for `Runner`-managed agents. An explicit
  `Runner.run(..., workspace_path=...)` is injected as supplied; when omitted,
  the runner injects its resolved managed run workspace.
- Direct callers of `FeedbackLearnerAgent` may pass `workspace_path` when that
  lower-level helper should receive workspace context.
- There are no breaking changes in this patch release.

### Known Limitations

- `FeedbackLearnerAgent` is not owned by `Runner`, so direct calls without
  `workspace_path` do not receive a workspace section.

### Verification

- `uv run --extra dev --extra mcp --frozen pytest`
- `uv build`
- `uv run --with twine python -m twine check <distributions>`
- `git diff --check`

## 0.7.5

### Added

- Static and dynamic DAG agent capability calls accept optional
  `reference_content`. Non-empty content is supplied in a separate user-message
  section as task data; empty content does not alter the assembled prompt. The
  schema-driven WebUI exposes the argument and supports binding it to graph
  input, artifacts, or upstream node output.
- Web static-DAG string arguments recognize `{{ variable }}` templates. The
  template editor can insert and directly bind a selected variable at the
  cursor; manually typed placeholders require an explicit picker selection.
  The editor persists the existing structured `format` expression, so SDK and
  API request shapes do not change. Existing format expressions that cannot
  round-trip through the visual syntax remain unchanged and read-only.
- Web DAG canvases distinguish conditional edges and show compact condition
  labels. The static-DAG edge inspector edits truthiness and comparison
  conditions with scoped variable pickers; unsupported expressions remain
  read-only and are preserved unchanged.

### Changed

- The internal `typed_spec` planner response now contains executable intent
  only. Graph names and descriptions, node titles, and edge reasons are no
  longer model-generated fields. The host supplies canonical graph identity;
  display-only changes no longer invalidate completed DAG results.
- The default `typed_spec` planner contract now accepts capability nodes only;
  map, subgraph, loop, and item-expression authoring remains available through
  `sdk_builder` and the public static-DAG SDK. Canonical `DAGSpec` execution
  support is unchanged.
- Structured planner calls now use one transport path: the complete compact
  schema is included in the system prompt, and the built-in OpenAI-compatible
  provider requests `response_format.type="json_object"`. The
  `structured_output_mode` SDK, YAML, API, and WebUI setting has been removed.

### Migration

- Existing agent nodes require no changes because `reference_content` defaults
  to an empty string.
- Deterministic providers that emit `typed_spec` planner fixtures must remove
  the deleted display-only fields and replace map, subgraph, or loop proposals
  with capability-node graphs. Use `sdk_builder` when model-authored complex
  control flow is required. Canonical `DAGSpec` and public static-DAG SDK shapes
  are unchanged.
- Remove `structured_output_mode` from Provider construction, configuration
  files, and model-management payloads.

### Verification

- `uv run --extra dev --extra mcp --frozen pytest`
- `npm --prefix web test`
- `npm --prefix web run build`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`

## 0.7.4

### Added

- `Runner(..., mcp_stdio_stderr="inherit")` lets a supervising host explicitly
  forward stdio MCP server stderr to the host process. `Runner.from_config(...)`
  accepts the same explicit host-policy argument, and derived runners inherit
  their parent's setting unless it is overridden.

### Changed

- stdio MCP server stderr is discarded by default. The SDK no longer creates
  or appends to `~/.dagent/logs/mcp-stderr.log`, because server stderr may
  contain credentials or other sensitive values.

### Migration

- Hosts that previously inspected the SDK-created MCP stderr file must instead
  opt into `mcp_stdio_stderr="inherit"` and capture process stderr at their own
  bounded, sanitized supervision boundary. Hosts that do not need MCP stderr
  require no changes.

### Verification

- `uv run --extra dev --extra mcp --frozen pytest`
- `git diff --check`

## 0.7.3

### Added

- Dynamic DAG planning now uses a strict internal JSON Schema contract with
  explicit `propose_plan`, `no_change`, and `final_answer` actions.
- Typed dynamic plans support capability/agent, map, subgraph, and bounded loop
  nodes; conditional edges; artifacts; graph output; and recursive value
  expressions. They normalize to canonical `DAGSpec` before review/execution.
- `RunState.dag_spec` and `PendingReview.proposed_dag_spec` retain canonical
  dynamic specs in SDK checkpoints and review continuations.
- `dagent.providers.StructuredOutputFormat` is the provider-neutral structured
  response contract. `ChatResponse.refusal` carries provider refusals.
- `Runner(..., planner_frontend="sdk_builder")` and the matching top-level YAML
  setting enable an optional restricted SDK Builder planner. It covers initial
  planning and full-spec replanning without executing model-generated Python.
- New V2 checkpoints record `planner_frontend`; Builder plans freeze the
  versioned `generate-dag` skill content and digest for deterministic resume.

### Changed

- Dynamic planners reference stable capability ids directly. The host fills
  capability kind, defaults, risk, boundaries, and invocation identity.
- Full-spec replanning preserves unchanged invocation ids, requires
  `rerun_nodes` for changed completed nodes, and invalidates changed downstream
  results. Pending DAG reviews persist `rerun_nodes`, and artifact definition
  changes participate in review and invalidation decisions.
- `OpenAICompatibleProvider` maps planner schemas to Chat Completions
  `response_format.type="json_schema"` for streaming and non-streaming calls.
- The local API applies `planner_frontend` as a service-wide Runner setting.
  Request payloads and the WebUI have no per-request frontend selector.
- The WebUI renders a bounded recent-message window for long conversations and
  loads older messages incrementally, while memoizing conversation-history
  sections to reduce unrelated rerenders.

### Fixed

- `Runner.cancel(run_id)` now propagates cancellation through active runtime
  work to async capabilities, MCP calls, and the built-in shell process group.
  The WebUI stop control calls the backend cancellation endpoint instead of
  only closing its local event stream.
- Rejected or otherwise settled review requests are no longer restored as
  pending review dialogs when a historical conversation is reopened.
- Typed planner schemas now preserve required planner fields and use strict
  structured-output unions supported by the provider. Replanning correctly handles
  artifact revisions, explicit reruns across review checkpoints, changed
  artifact state, and schema-less nested inputs.
- Builder replanning preserves user-owned argument keys that match host field
  names, validates paths through composite subgraph outputs, and always sends a
  complete canonical DAGSpec as authoritative replan context.

### Breaking Changes

- The free-form PlanSpec DSL and its parser/compiler types have been removed.
  Dynamic planner responses are JSON only; there is no legacy fallback.
- Custom `ChatProvider` implementations must accept the keyword-only
  `response_format` argument on both `chat(...)` and `stream_chat(...)`, and
  must honor it for dynamic DAG planning.
- Review checkpoints created from the removed PlanSpec path do not contain a
  canonical proposed spec and cannot resume as typed DAG reviews.

### Migration Steps

- Update deterministic providers and test fixtures to return schema-valid
  planner JSON instead of PlanSpec text or bare `NO_CHANGE`/final text.
- Update custom providers to pass through `StructuredOutputFormat`, return JSON
  matching its schema, and surface structured-output refusals in
  `ChatResponse.refusal`.
- Recreate pending dynamic DAG reviews using this version before persisting new
  checkpoints; do not attempt to convert old PlanSpec review state implicitly.
- No action is required for existing V1 checkpoints; they remain readable and
  use `typed_spec`. Persist newly emitted V2 checkpoints without dropping their
  frontend or frozen planner-skill fields.

### Verification

- `uv run --extra dev --frozen pytest`
- `npm --prefix web test`
- `npm --prefix web run build`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`

### Known Limitations

- Phase one replans with a complete typed graph; atomic typed patches are
  deferred.
- `typed_spec` remains the default. `sdk_builder` accepts only the documented
  straight-line AST subset; it is not a general Python authoring environment.
- Atomic typed or Builder patch operations remain deferred; both frontends
  replan with a complete graph.

## 0.7.2

### Changed

- The built-in `conversation` profile is now presented as the General-Purpose
  Agent and more clearly prefers a directly relevant available tool for simple
  tasks while reserving `dag_agent` for complex orchestration.

### Fixed

- `tool.shell` now starts commands in a dedicated process group and terminates
  the entire group on timeout, including children in pipelines. Timeout cleanup
  is bounded even when an escaped process retains an inherited output pipe, and
  captured output remains available in the terminal timeout error.

### Breaking Changes

- None.

### Migration Steps

- No migration action is required for this patch release. Workflows that must
  invoke a specific tool should continue to use a dedicated custom profile;
  built-in profile tool selection remains model-directed.

### Verification

- `uv run --extra dev --frozen pytest`
- `uv build`
- `uv run --with twine python -m twine check dist/*`
- `git diff --check`

### Known Limitations

- The General-Purpose Agent's direct-tool preference is prompt guidance, not a
  forced invocation guarantee.

## 0.7.1

### Added

- `ResolvedRunPlan` records the target-specific profiles, local loop limits,
  exact capability/agent/skill scope, validation settings, and run-wide limits
  resolved by the SDK. It contains no handlers, provider secrets, connections,
  or host policy.
- `RunCheckpoint` combines `RunState`, `ResolvedRunPlan`, and cumulative
  `ExecutionUsage`. SDK-produced results expose it as `result.checkpoint`, and
  `Runner.run_checkpoint(run_id)` returns the latest in-memory or terminal
  checkpoint.
- `ExecutionLimits` provides independent total-operation, model-turn, and
  capability-call ceilings. Reservations are shared by root agents, DAG work,
  validation, retries, concurrent branches, and subagents.
- `ExecutionLimitExceeded` fails before an operation that would cross a limit.

### Changed

- Cross-process review continuation now uses
  `Runner.resume(..., checkpoint=...)` or
  `Runner.resume_stream(..., checkpoint=...)`. Resume validates exact scope,
  reconstructs a target-specific derived runtime, and restores usage before
  applying the review decision.
- `Runner.run(..., checkpoint=...)` restores usage and original limits for a
  normal cross-process continuation. Same-Runner `state=...` continuation uses
  the matching checkpoint cache and rejects stale state.
- Resolved profile snapshots are deeply frozen. Every plan carries a canonical
  SHA-256 fingerprint, and checkpoint hydration rejects inconsistent pending
  capability reviews or invocations outside the resolved scope.
- Checkpoint review decisions are one-shot within a `Runner`. A resume failure
  records a terminal checkpoint; budget exceptions expose it with updated
  usage on `error.checkpoint` and `error.usage`.
- SDK-produced `RunState.capability_scope` now records resolved canonical skill
  IDs and exact capability IDs rather than an unresolved declaration.
- `RunResult.model_dump(...)` retains its previous `state` and `output_text`
  payload shape. Persist `result.checkpoint` separately for portable review
  continuation.

### Breaking Changes

- `Runner.resume(..., state=...)` and `resume_stream(..., state=...)` are
  deprecated and emit `DeprecationWarning` when no matching in-memory
  checkpoint exists. They remain as an explicit v0.7.1 legacy path, but cannot
  restore target-specific execution semantics.
- Checkpoint resume does not fall back to the base runtime when a required
  capability or skill is unavailable; it fails closed.
- Restored limits cannot be replaced or enlarged. Durable multi-process hosts
  must atomically claim review IDs before execution to prevent stale-checkpoint
  replay across processes.

### Migration Steps

- Persist `result.checkpoint.model_dump_json()` for Runs awaiting review.
- Restore with `RunCheckpoint.model_validate_json(...)`, construct a compatible
  `Runner`, and pass the checkpoint to `Runner.resume(...)`.
- For an ordinary bounded continuation after a restart, pass the restored
  checkpoint to `Runner.run(...)`, not only its state.
- Map a host-wide step policy to `ExecutionLimits.max_total_operations` when
  desired. Do not rewrite `ToolAgent.max_steps` or `DagAgent.max_cycles`; those
  remain local loop controls.
- Keep checkpoint storage, tenant authorization, retention, and compatible
  capability/provider construction in the host.

### Verification

- `uv run --extra dev pytest`
- `uv build`
- `git diff --check`

### Known Limitations

- A checkpoint describes execution semantics, not executable handlers or
  provider connections. The restoring host must construct a compatible
  `Runner`.
- The plan fingerprint detects accidental mutation but is not a signature;
  checkpoint authenticity remains a host responsibility.
- Static DAG results now resume supported direct agent-node tool reviews through
  `Runner.resume(..., checkpoint=...)`. `MapNode`, `Subgraph`, and `LoopNode`
  agent combinations are explicitly rejected; general static DAG crash
  continuation remains unsupported.

## 0.7.0

### Added

- `Runner.derive(...)` can inherit local `CapabilityBinding` registrations with
  `inherit_local_tools=True` and exclude caller-owned tool IDs with
  `exclude_local_tool_ids`.
- `Runner.run(...)` and `Runner.stream(...)` accept caller-supplied Run IDs;
  duplicate new-Run IDs on one `Runner` are rejected.
- Versioned `RunState` schema v1 supports JSON round trips and explicit
  same-Runner or caller-restored resume with `Runner.resume_stream(...)`.
- Capability references are validated without mutating caller-owned agents,
  DAGs, profiles, or bindings; validation issues remain typed after hydration.
- SDK-produced MCP snapshots support fail-closed lazy connection while
  preserving canonical capability identity, current filters, and current
  policy.

### Changed

- dagent is again an in-process SDK library. Process lifetime, command
  protocols, health checks, persistence, credentials, scheduling, and
  container lifecycle belong to the calling host.
- Caller-supplied Run IDs, versioned `RunState`, capability-reference validation,
  MCP snapshots, lazy MCP connection, and `Runner` cleanup remain supported
  public library behavior.

### Fixed

- Lazy MCP registration now fails when an enabled server has no snapshot,
  rejects non-canonical snapshot identity, and reapplies current filters and
  policy.
- Concurrent first MCP calls now share one serialized server startup.
- Distinct new Runs on one `Runner` cannot reuse a caller-supplied Run ID.
- `CapabilityBinding` conflicts and hydrated `ValidationIssue` objects retain
  machine-readable issue data.
- Stream cancellation waits for the SDK-owned execution task to finish
  cleanup.

### Breaking Changes

- Removed `RuntimeRunSpec`, `RuntimeFrame`, the runtime transport schemas, and
  `python -m dagent.worker`. The historical v0.6.9 tag remains unchanged.
- This is an intentional pre-1.0 removal. The project knows of no v0.6.9
  process-API adopters, but callers using that API must migrate.

### Migration Steps

- Construct `Runner` in the host process and call `Runner.stream(...)` for a
  new execution.
- Continue a pending review with `Runner.resume_stream(...)` and explicit
  `RunState` when caller-managed recovery requires it.
- Keep process commands, credentials, persistence, retries, health, and
  container lifecycle in the host.
- Persist SDK-produced MCP snapshots if the host wants fail-closed lazy MCP
  registration; do not reconstruct MCP capability IDs outside the SDK.

### Verification

- `uv run --extra dev pytest`
- `uv build`
- `git diff --check`

### Known Limitations

- dagent does not provide a process host, service loop, durable store, or
  transparent active-Run recovery.

## 0.6.8

### Added

- `dagent.capabilities.python_tools` now provides SDK-owned helpers for loading
  configured `@dagent.tool` Python sources from explicit path, managed, or
  module entries.
- `Runner.reload_python_tool_sources(...)` loads configured Python tool sources
  and returns a stable registration result without exposing executable bindings.
- `Runner.derive(...)` creates an independent runner with explicit provider,
  workspace, MCP server, Python tool, agent, profile, sandbox, and validation
  overlays.
- `Runner.mcp_server_snapshot(...)`,
  `Runner.list_mcp_server_snapshots()`,
  `Runner.reload_mcp_servers_with_snapshots(...)`, and
  `Runner.catalog_view(...)` expose read-only runner-owned capability and MCP
  registration views without handler or catalog internals.

### Changed

- The local API/WebUI backend now uses SDK-owned Python tool loading helpers
  instead of maintaining a separate loader implementation.
- Architecture guidance now explicitly discourages legacy-test compatibility
  code, duplicate implementations, broad fallback behavior, and enterprise
  concerns in the installable SDK.

### Fixed

- The architecture boundary test no longer fails on obsolete `tool_review`
  wording in persistence test names.

### Breaking Changes

- None for the public Python SDK.

### Migration Steps

- SDK users do not need to change existing code.
- Hosts that previously loaded configured Python tools or inspected MCP/catalog
  internals should migrate to the new `Runner` methods and keep RBAC,
  redaction, persistence, and effective-configuration composition in the host
  layer.

### Verification

- `uv run --extra dev pytest`
- `uv build`
- `git diff --check`

### Known Limitations

- `Runner.catalog_view(...)` is a runtime registration view, not a user-facing
  authorization surface. Hosts remain responsible for RBAC, redaction, and
  policy filtering before returning catalog data to users.

## 0.6.7

### Added

- None.

### Changed

- Tool-agent and dynamic DAG LLM calls now retry transient provider failures
  and request timeouts with increasing waits before surfacing the existing
  failure path.
- Default LLM retry waits remain `1`, `2`, `5`, `10`, and `30` seconds.

### Fixed

- Permanent LLM provider failures, such as invalid request payloads or
  non-retryable client errors, are no longer retried.
- Streaming LLM calls no longer retry after response tokens have been emitted,
  avoiding duplicated partial stream output.

### Breaking Changes

- None for the public Python SDK.

### Migration Steps

- No SDK migration action is required.

### Verification

- `uv run --extra dev pytest`
- `uv build`
- `git diff --check`

### Known Limitations

- LLM retry classification is intentionally conservative. Custom providers
  should surface timeouts, connection errors, retryable status codes, or
  provider-specific transient exception names for automatic retries.

## 0.6.6

### Added

- The local WebUI can edit Office documents through ONLYOFFICE for project
  files and run artifacts when the corresponding edit settings are enabled.
- Dynamic and static orchestration workspaces now persist run history, support
  historical run inspection, and can delete stored run history entries.
- Chat and dynamic DAG conversations now persist visible message timelines so
  conversations can be hydrated directly after API restarts.
- `examples/local_test_mcp.py` provides a local stdio MCP server for
  registration and tool-call timeout diagnostics.

### Changed

- MCP tool calls now default to a `300` second timeout, and the local WebUI MCP
  form exposes the tool timeout setting.
- Project files and run artifact metadata now include file `version` values so
  Office previews can refresh when backing files change.

### Fixed

- MCP connection and tool-call timeouts now report explicit timeout messages
  instead of empty error text.
- Orchestration run history rows, hydration, run summaries, and workspace
  separation have been tightened across dynamic, static, standalone, and
  project-scoped flows.
- Interrupted chat and persisted dynamic DAG history hydration handle completed
  traces and visible turns more consistently.

### Breaking Changes

- None for the public Python SDK.

### Migration Steps

- No SDK migration action is required.
- MCP users who rely on the previous shorter implicit tool-call cutoff can set
  `tool_timeout` explicitly in their MCP server config.
- Existing local WebUI SQLite storage may be recreated if it was produced by an
  incompatible pre-release schema.

### Verification

- `uv run --extra dev pytest`
- `npm --prefix web test`
- `npm --prefix web run build`

### Known Limitations

- The local storage backend is SQLite plus local filesystem workspaces. Cloud
  or multi-worker deployments still need the planned Postgres, object-storage,
  and worker execution backends.

## 0.6.5

### Added

- The local API/WebUI project file browser can request a recursive project file
  tree with nested preview and download metadata while skipping unsafe workspace
  escapes.
- The WebUI now summarizes completed chat process timelines so final answers
  stay visible while reasoning, validation, and capability activity remain
  inspectable.

### Changed

- MCP server registration and tool calls now share explicit default timeouts:
  `connect_timeout` defaults to `60` seconds and `tool_timeout` defaults to `90`
  seconds for stdio and Streamable HTTP servers.
- The WebUI project workspace, static DAG workspace, chat drafts, run settings,
  and completed-run traces have been refined for denser navigation and steadier
  layouts.

### Fixed

- Persisted chat trace hydration now replays stored stream envelopes through the
  same dispatcher as live streams, preventing missing or inconsistent completed
  traces after refresh.
- Local API workspace file URIs now use platform-aware file URI handling, fixing
  Windows workspace paths.
- Project file tree listing skips symlink workspace escapes and avoids directory
  cycles.

### Breaking Changes

- None for the public Python SDK.

### Migration Steps

- No SDK migration action is required.
- MCP users who rely on the previous shorter implicit tool-call cutoff can set
  `tool_timeout` explicitly in their MCP server config.

### Verification

- `uv run --extra dev pytest`
- `npm --prefix web test`
- `npm --prefix web run build`

### Known Limitations

- The local storage backend is SQLite plus local filesystem workspaces. Cloud
  or multi-worker deployments still need the planned Postgres, object-storage,
  and worker execution backends.

## 0.6.4

### Added

- The local API/WebUI now persists dynamic and static DAG orchestration sessions,
  including draft DAG state, selected-node UI state, saved static DAG links, run
  event history, and run state snapshots.
- Saved static DAGs now preserve saved-record metadata, revisions, project
  ownership, editor layout, and persisted artifact uploads across API process
  restarts.
- Static orchestration run timelines can be restored from durable run events
  after a run completes.

### Changed

- The local API/WebUI store now isolates chat, dynamic DAG, and static DAG
  conversations by kind; ordinary chat streams reject orchestration
  conversations.
- The static orchestration UI keeps saved DAG display names and visible revision
  state stable across name edits, refreshes, and run completion.
- The WebUI now enters an empty chat when opening the chat workspace instead of
  automatically selecting an existing conversation. Artifact panels can still
  expand when artifacts exist without auto-selecting a preview file.
- The non-public legacy `/dags/{dag_id}/run/stream` local API route was removed.
  Persisted static DAG runs use `/saved-dags/{dag_id}/run/stream`.

### Fixed

- The dynamic orchestration page now keeps its history and run workspaces
  separate from projects. Project-scoped DAG conversations remain in the smart
  workbench project flow.
- Static orchestration run events and final timelines no longer disappear after
  run completion.
- Static orchestration hydration no longer replays saved DAG state over the
  current editor and completed run results after a save or conversation refresh.
- Concurrent static run attempts no longer silently lose orchestration session
  creation when the same conversation session is created by another request.

### Breaking Changes

- None for the public Python SDK.

### Migration Steps

- No SDK migration action is required.
- Incompatible pre-release local SQLite API databases are recreated instead of
  migrated. This does not affect the public Python SDK.

### Verification

- `uv run --extra dev pytest`
- `npm --prefix web test`
- `npm --prefix web run build`

### Known Limitations

- The local storage backend is SQLite plus local filesystem workspaces. Cloud
  or multi-worker deployments still need the planned Postgres, object-storage,
  and worker execution backends.

## 0.6.3

### Added

- The local API now persists projects, standalone conversations, project
  conversations, run state snapshots, run event history, and review records in
  the API storage layer without changing the public SDK.
- The WebUI now separates standalone conversations from projects. Projects can
  be expanded to show their conversations, and each project exposes a detail
  workspace with file management, directory browsing, upload, rename, delete,
  download, preview, and document-preview configuration support.
- Persisted chat streams can resume from stored `RunState` snapshots, including
  pending reviews, artifact manifests, traces, and project conversation state
  after an API process restart.
- Standalone conversations now use a durable workspace under
  `.dagent/projects/_conversations/<conversation_id>/workspace`; project
  conversations share the project workspace under
  `.dagent/projects/<project_id>/workspace`.

### Changed

- Persisted conversations are single-writer per conversation. The API uses
  conversation locks with leases so another stream cannot drive the same
  conversation concurrently.
- Deleting a standalone conversation removes its database rows and its
  conversation root directory. Deleting a project conversation removes the
  conversation and run records while preserving the shared project workspace.
- Project deletion removes the project database records and the local project
  root directory after checking that no project conversation is active.
- The system settings label for OnlyOffice has been renamed to Document
  Configuration in the WebUI.

### Breaking Changes

- None for the public Python SDK.

### Migration Steps

- No SDK migration action is required.
- Existing local one-off runs and static/dynamic orchestration runs remain in
  their previous run workspaces. New persisted chat conversations use the
  `.dagent/projects/...` workspace layout.
- If you tested an unreleased 0.6.3 development branch before this release, you
  can remove any stale empty directories under `.dagent/projects/_conversations`
  that were created before standalone conversation root cleanup was added.

### Verification

- `uv run --extra dev pytest`
- `npm --prefix web test`
- `npm --prefix web run build`

### Known Limitations

- Static DAG runs and the dynamic orchestration workspace still use the existing
  run workspace model unless a workspace root is explicitly provided. They have
  not yet been folded into project/conversation persistence.
- The local storage backend is SQLite plus local filesystem workspaces. Cloud
  or multi-worker deployments still need the planned Postgres, object-storage,
  and worker execution backends.

## 0.6.2

### Added

- The WebUI now supports browser previews for additional artifact types,
  including Office documents and PPTX artifacts.
- The local API and WebUI expose OnlyOffice configuration for richer document
  preview workflows when an OnlyOffice server is configured.
- OnlyOffice-backed DOCX, XLSX, and PPTX previews can now be switched into edit
  mode independently for project files and run artifacts. Edit mode disables
  autosave and only writes back when the user clicks Save.
- Run artifact and project file metadata now include a `version` field derived
  from file size and nanosecond mtime for precise preview cache invalidation.
- View-only ONLYOFFICE previews keep a small browser-side cache of recently
  opened editor instances to speed up switching between unchanged documents.
- Chat workbench uploads can now be attached to a message and materialized into
  the run workspace so agents can inspect user-provided files.
- Static DAG output binding and schema-argument editing support have been
  expanded in the WebUI.
- Tools and MCP resources now use richer tree-style navigation in the WebUI.

### Changed

- Artifact preview chrome, artifact-tree interactions, and collapsed artifact
  rail behavior have been refined for denser workspace use.
- Workbench upload handling is stricter about filenames and workspace
  boundaries.
- OnlyOffice preview URLs now use signed file tokens that also carry whether the
  current preview session may save edits. The callback handler only overwrites
  files for user-triggered force-save callbacks.

### Breaking Changes

- None.

### Migration Steps

- No migration action is required for this patch release.
- To use Office document previews or editing, configure an OnlyOffice document
  server in the WebUI system settings. Editing remains off until the project-file
  or run-artifact edit switch is enabled.

### Verification

- `uv run --extra dev pytest`
- `source ~/.nvm/nvm.sh && npm --prefix web test`
- `source ~/.nvm/nvm.sh && npm --prefix web run build`

### Known Limitations

- Office previews require an external OnlyOffice document server.
- Editing run artifacts mutates files in the run workspace but does not rewrite
  stored trace history.
- Workbench uploads are materialized into the local run workspace; they are not
  persisted across ephemeral container storage without a persistent volume.

## 0.6.1

### Added

- The local WebUI now persists user-managed model providers, the active model,
  user MCP servers, and explicitly imported Python tool sources through the
  user config file.
- The WebUI can import Python tools from local paths or uploaded `.py` files,
  manage their enabled state, validate them, reload them, and remove uploaded
  managed files.
- The Python tool import dialog now discovers top-level functions decorated
  with `@tool` or `@dagent.tool` and auto-fills the function-name list while
  keeping the list editable.
- The WebUI exposes MCP servers in the MCP service view but no longer lists MCP
  capabilities in the generic tools view.

### Changed

- Capability definitions now separate stable ids from call names. `id` remains
  the execution identity; `name` is the LLM/PlanSpec function name; and
  `display_name` is UI-only text.
- Runner.add_tools is now atomic: if any binding in a batch cannot be
  registered, the runner leaves the catalog unchanged. Re-registering an
  identical existing binding remains idempotent.
- Local WebUI Python tool entries using `source: "module"` no longer reload
  modules that are already present in `sys.modules`. Use `path` or uploaded
  `managed` sources when you need reload-style development behavior.
- `/python-tools/reload` now reloads only imported Python-tool capabilities. It
  no longer restarts the whole runner or reconnects unrelated MCP servers;
  presets that reference removed Python tools are reported as agent errors.

### Breaking Changes

- `@dagent.tool` still does not accept `id=`. Python function tools always
  derive their capability id from the function name as `tool.<function_name>`.
  `name=` is accepted again, but it controls only the LLM/PlanSpec function
  name; it does not change the capability id.
- `CapabilityDefinition.name` and `CapabilityDefinition.display_name` are
  public fields. If omitted, `name` defaults to the capability id with dots
  replaced by underscores, and `display_name` defaults to `name`.
- Raw `CapabilityDefinition.id` values must use dotted capability ids beginning
  with `tool`, `agent`, `mcp`, `skill`, or `memory`. Each segment may contain
  only letters, numbers, and underscores; at least two segments are required.
- LLM-visible PlanSpec and tool-call function names now use
  `CapabilityDefinition.name`. Update saved dynamic DAG PlanSpec text and
  deterministic provider fixtures if you set custom names.

### Migration Steps

- If you use custom capability `name` values, update saved dynamic DAG PlanSpec
  text and deterministic provider fixtures to call those names.
- Inspect raw capability definitions and saved allowlists for dotted capability
  ids that do not start with `tool`, `agent`, `mcp`, `skill`, or `memory`.
- Rename WebUI-managed profiles, agent presets, and user MCP server keys that
  contain dashes or other characters outside letters, numbers, and underscores.
- Use `path` or uploaded `managed` Python tool sources for reload-style local
  development; `module` sources reuse already imported modules.

### Verification

- `uv run --extra dev pytest tests/test_api.py tests/test_python_tool_imports.py -q`
- `source ~/.nvm/nvm.sh && npm --prefix web test`
- `source ~/.nvm/nvm.sh && npm --prefix web run build`

### Known Limitations

- Python tool auto-discovery recognizes literal `@tool` and `@dagent.tool`
  decorators. If a source imports either name through an alias, enter the
  function names manually.
- Invalid legacy WebUI agent preset files are reported as errors and are not
  migrated automatically.

## 0.6.0

### Added

- Single-level subagent delegation is now part of the public SDK.
  `Runner.add_agent(...)` and `Runner.add_agents(...)` register leaf
  `ToolAgent` configurations as `agent.<name>` capabilities.
- Top-level `ToolAgent`, `AutoAgent`, and `DagAgent` runs can expose selected
  subagents through `agents=["agent.<name>"]`, direct `ToolAgent` objects, or
  `agents="registered"` for all agents registered on the runner.
- Static `Dag` nodes can target `ToolAgent` objects in Python. Managed API/WebUI
  agent presets are exposed as `agent.<name>` capabilities, with their selected
  tools, MCP capabilities, and skills mapped onto public `ToolAgent` fields.
- The new `examples/agent_delegation.py` example demonstrates registering a leaf
  subagent and exposing it to a top-level run.

### Changed

- LLM-visible function names are now consistently derived from stable
  capability ids by replacing dots with underscores. For example,
  `tool.search` becomes `tool_search(...)`, and `agent.helper` becomes
  `agent_helper(...)`.
- Runner capability registration now keeps registered subagent runtime scopes
  aligned as tools, MCP servers, raw capabilities, and skill visibility change.
- Local API agent preset payloads now use public `ToolAgent` field names and
  enforce the same leaf-subagent constraints as the SDK.

### Breaking Changes

- `@dagent.tool` no longer accepts `id=` or `name=`.
  Python function tools always derive their capability id from the function
  name as `tool.<function_name>`. Rename the function or wrap the implementation
  with a differently named function when changing the public id.
- `CapabilityDefinition.name` has been removed. Raw capability definitions now
  carry only `id` as their stable public identifier; LLM-visible function names
  are derived from that id.
- Raw `CapabilityDefinition.id` values must use supported dotted capability id
  forms: `tool.<name>`, `agent.<name>`, `mcp.<server>.<tool>`, `skill.<name>`,
  or `memory.<name>`. Each segment may contain only letters, numbers, and
  underscores; leading or trailing whitespace is rejected.
- LLM-visible PlanSpec and tool-call function names are now derived from
  capability ids by replacing dots with underscores. For example, use
  `tool_search(...)`, `tool_shell(...)`, and `agent_helper(...)` instead of
  short names such as `search(...)`, `shell(...)`, or `helper(...)`. Update
  saved dynamic DAG PlanSpec text and deterministic provider fixtures
  accordingly.
- MCP capability ids now use stable canonical keys for raw MCP server or tool
  names that are not already valid id segments. For example, `mock-server` no
  longer maps to `mock_server`; inspect registered capability definitions and
  update saved capability allowlists or DAG specs.
- Local API managed profile and agent preset names may contain only letters,
  numbers, and underscores. Replace dashes with underscores before creating new
  managed profiles or agent presets.
- Local API MCP server names are strict workspace keys and may contain only
  letters, numbers, and underscores. This does not restrict third-party MCP tool
  names, which are preserved in capability config.
- Local API agent preset JSON now uses `ToolAgent` field names. Replace
  `capability_ids` with `capabilities`; registered presets must keep `agents`
  empty and `review` set to `"fast"`. Old preset files are reported as errors
  and are not migrated automatically.

## 0.5.2

- Breaking change: `Runner(...)` and `Runner.from_config(...)` now default their
  workspace to `.dagent`, and each run records a workspace under
  `.dagent/runs/<run_id>` unless `workspace` or `workspace_root` is set
  explicitly.
- Breaking change: built-in file and shell tools now resolve relative paths from
  the current ToolAgent or DagAgent message run workspace, not from the runner
  workspace root. A `tool_write_file(path="notes.txt", ...)` call in a normal
  agent run now writes under `.dagent/runs/<run_id>/notes.txt` instead of
  `.dagent/notes.txt`. Code that expects files directly under the runner
  workspace should pass absolute paths or move shared inputs into each run
  workspace. Static DAG artifact paths still use their documented artifact
  mapping; pass `artifact.path` for built-in path-aware tools.
- Breaking change: `Boundary` now declares only `allowed_paths`. Remove
  `mode=` and `allowed_commands=` from SDK code, saved DAG specs, and API
  payloads. Shell command safety checks are enforced by the built-in shell tool
  rather than by per-node `allowed_commands`.
- MCP configs now support Streamable HTTP servers through explicit
  `transport: "http"`, `url`, and optional `headers`. Header values expand
  `${ENV_NAME}` at connection time. The optional MCP extra now requires
  `mcp>=1.27.1,<2`.

## 0.5.1

- No migration action is required for this patch release. It adds WebUI model
  provider management and API key redaction improvements without changing the
  documented public SDK contracts.

## 0.5.0

- Sandbox execution now fails closed for unsupported targets and capabilities.
  Only built-in tool capabilities execute inside `execution="sandbox"`. Python
  function tools, raw registered capabilities, MCP, skills, memory, agents,
  DAGs, `DAGSpec`, and `DagAgent` must use `execution="local"` until sandbox
  support is added for them; they no longer fall back to host execution when a
  sandbox run is active.

## 0.4.2

- The built-in shell command capability is now `tool.shell`, with DAG DSL calls
  written as `tool_shell(command="...", cwd=".")`. Replace saved
  `tool.run_command` capability ids and `run_command(...)` plan calls before
  upgrading. No legacy alias is registered.

## Public Surface Expectations

Treat the following as released behavior:

- package install name: `dagent-ai`
- Python import name: `dagent`
- public exports listed in [Python SDK Reference Map](python-sdk.md)
- Python tool capability ids using `tool.<name>`
- MCP capability ids using `mcp.<server>.<tool>`
- explicit `Runner(...)` inputs
- config-file loading through `Runner.from_config(...)`
- static DAG explicit dependency requirements
- review-safe continuation through `Runner.resume(...)`

## Capability Ids

Python function tools use `tool.<name>`. Do not rely on old or internal
capability id prefixes as compatibility aliases.

## Runner Configuration

`Runner(...)` uses explicit SDK inputs and does not implicitly read
`config.yaml`. Use `Runner.from_config(...)` when loading provider settings, MCP
servers, validation, or profile directories from a config file.

## Profiles

Built-in profiles are packaged Markdown resources under
`dagent/resources/profiles/<name>.md`. User profile directories must be passed
explicitly through `profile_root`.

## Static DAG Dataflow

Static DAGs require explicit dependencies. A value reference such as
`node.output.title` does not create an edge. Add the dependency with
`dag.add_edge(...)`.

## Future Entries

When a future release changes documented public behavior, add:

- affected version
- old behavior
- new behavior
- migration steps
- related examples or docs
