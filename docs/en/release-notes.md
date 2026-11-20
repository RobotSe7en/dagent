# Release notes

## 0.11.2 — clearer user interaction tools

Install with `pip install --upgrade 'dagent-ai==0.11.2'`. Existing 0.11.1
applications can [upgrade directly](migration.md#0112).

- Describe the parameters and return values of all four `ui_tools()` bindings
  for models, including the IDs and labels in select options.
- Clarify that `tool.ui_editor` opens an interactive editor with the full initial
  content and returns the user's submission. See [User Interactions](user-interactions.md).

Release verification included 2,605 passing Python tests with the dev and MCP
extras and 128 passing frontend tests. The frontend build, wheel and sdist
metadata checks, and offline interaction example using the built wheel also passed.
Two live MiniMax tests and one Docker integration test were skipped.

## 0.11.1 — opt-in user interactions

Install with `pip install --upgrade 'dagent-ai==0.11.1'`. This release adds
explicitly enabled user interactions without breaking changes. Existing 0.11.0
applications need no migration; see the [0.11.1 migration notes](migration.md#0111)
to enable the new feature.

- Add per-execution `UserInteractionOptions`, public host response/abort methods,
  four registerable `tool.ui_*` tools, and `ToolContext.ui` for async Python tools.
- Continue the original local root ToolLoop invocation after a typed response;
  distinguish abandonment, False, and empty text. Reject steering while waiting.
- Isolate waits across runs and clean them on stop, close, failure, and deadlines.
  Timeouts and host unavailability fail the run instead of becoming tool results.
- Preserve result storage, overflow references and persistence ACK gates. Existing
  defaults, records and toolsets are unchanged; applications that do not enable
  interactions need no migration.
- Preserve existing `run`/`resume` overrides in ordinary streams and keep independent
  inner-run interaction failures from terminating the calling run.
- Initial support is local root ToolLoop only. DAGs, delegated agents and sandbox
  interaction are excluded; receivers and waits do not survive restart.
- Add an offline host example and regression coverage for values, identity,
  cancellation, review resume, steering, long results and persistence ordering.
  See [User Interactions](user-interactions.md).

Release verification: the full Python suite (dev and MCP) passed with 2,604 tests
and 3 skips. The offline user interaction, persistence and retention examples,
all 128 frontend tests, the frontend build, wheel/sdist builds, Twine metadata,
version consistency, lockfile and diff checks passed. The built wheel passed an
isolated import and interaction smoke check with its bundled profile; all 107
public 0.11.0 exports remain available. Two opt-in live MiniMax tests and one
Docker test without a running daemon were skipped; live provider and Docker
acceptance require their respective environments.

## 0.11.0 — ToolLoop persistent stream V2

Install with `pip install --upgrade 'dagent-ai==0.11.0'`. This release includes
breaking changes to persistence callbacks, default workspaces and file storage;
follow the [0.11.0 migration steps](migration.md#0110) before upgrading hosts
that depend on them. ConversationState V4, RunState V6 and RunCheckpoint V9
records remain readable.

- Add opt-in `StreamPersistenceOptions` to `stream`/`resume_stream`, one mixed
  event stream, explicit ACK/rejection and an unbounded ordered event queue.
- Extend existing `ConversationChangeBatch` with terminal fields and the existing
  review checkpoint. No `RunSummary` or second checkpoint model is introduced.
- Execute ToolLoop and delegated tool agents without constructing diagnostic trace.
  Persist response/tool errors, usage, uncertain execution and resource references.
- Add public checkpoint review, detached pending-tool invocation and read-only
  validation APIs, plus public upload helpers in `dagent.artifacts`.
- Allow host-rebuilt context continuation and opt-in `input=None` retry; deactivate
  unfinished tail tool groups without fabricating results or replaying effects.
- Release acknowledged review state; ordinary state retention remains explicit.
- Keep confirmed conversation revisions consistent when cancellation races ACK.
  Cancel returns `False` once terminal delivery starts; review state is also
  released if that delivery is closed or rejected.
- Runner calls made by a tool inside a persistent Run without `persistence` run as
  independent ordinary runs instead of joining or breaking that Run's delivery.
- Unlimited retention no longer records used run ids, so a run that ended before
  saving state can again be retried with the same `run_id`.
- Persistent events enqueue without waiting for the reader or merging deltas;
  only durable batches wait for host ACK. Remove the draft `max_buffer_events`,
  `max_buffer_bytes` and `stall_timeout_seconds` options, overflow handling and
  queue-drain waits. `ack_timeout_seconds` remains supported.
- A review can be resumed as soon as its terminal batch is acknowledged, without
  waiting for the previous stream to end.
- A response retry right after a user message resends the same request instead of
  adding a second consecutive user turn.
- Tie delivery ownership to its producer so an old stream cannot block cancellation
  of an ordinary resume. Expired ACKs return `False` and never confirm a terminal.
- Retain final failed text-only responses in persistent context and child-agent
  failure excerpts; discarded attempts and failed calls remain audit-only.
- Let steer replace a trailing internal retry hint. Isolate nested `design_dag`
  calls, and end persistent readers cleanly on Runner close while preserving
  explicit reader-task cancellation.
- Deduplicate acknowledged, digested resource references with a bounded per-delivery
  cache; unversioned/mutable resources and new deliveries still notify the host.
- Pass execution mode explicitly through ToolLoop internals and centralize stream
  registration cleanup, preserving public interfaces and serialized records.
- Add opt-in `ToolAgent.response_retries` / `AutoAgent.response_retries`, disabled
  by default, with provisional stream output and `response.discarded` events.
- Add optional `Runner(max_retained_runs=...)` to release ended root-run state
  and owned DAG child sessions; unlimited retention remains the default.
- Keep separate tool trace nodes when call ids repeat across model rounds, and
  reject duplicate ids within one reply with `invalid_tool_call` before execution,
  including replies from custom providers.
- Reduce repeated context preparation, definition copying and archive writes;
  send incremental tool-loop messages and preserve published message ownership.
- **Breaking: workspaces and files.** Conversation runs now share a workspace
  chosen by `ConversationState.id`; static DAGs keep per-run workspaces. The SDK
  no longer carries, restores or verifies files between workspaces, and removes
  `ConversationResourceError`. Hosts moving a conversation must copy its files.
- Tool-output files are best-effort and require a model that can read them.
  `ContentReference.sha256`, `Attachment.sha256` and frozen-display recovery are
  optional; new tool-output/artifact/upload references carry no digest. Missing
  compaction archives degrade context, and display-write failures report storage
  warnings. Hosts own file integrity checks and must accept the optional fields.
- Tools in ToolAgent and dynamic DAG runs receive copies of ordinary container
  arguments; return values rather than mutating arguments to pass data back.
- **Breaking, explicitly authorized:** remove `on_conversation_change` from all
  four execution APIs, its Handler/Failure/Error contracts and the failure-event
  callback field. Remove DAG callback-only hooks. Other default execution,
  result, provider and persisted V4/V6/V9 contracts are preserved; V1 batch reads
  remain supported. See [migration](migration.md#0110).

Verification covers actual Chat Completions/Responses request equivalence,
public result and continuation contracts, review
recovery, retry/compaction, failure boundaries, slow consumers and memory release.
Release verification: the full Python suite (dev and MCP) passed with 2,535
tests and 3 skips; both offline persistence and retention examples passed.
All 128 frontend tests, the frontend build, wheel/sdist builds, Twine metadata,
version consistency, lockfile and diff checks passed. Two opt-in live MiniMax
tests and one Docker test without a running daemon were skipped; live provider
and Docker acceptance require their respective environments.

Limits: authoritative final messages still accompany display deltas; review V9
checkpoints retain full state and duplicated fields; there is no chunking or hard
frame bound. Unlimited ordinary retention and replay-ID growth remain explicit.
See [the persistence guide](conversation-changes.md) for host integration details.
