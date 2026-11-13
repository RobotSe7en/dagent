# Release notes

## Unreleased — ToolLoop persistent stream V2

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
- **Breaking, explicitly authorized:** remove `on_conversation_change` from all
  four execution APIs, its Handler/Failure/Error contracts and the failure-event
  callback field. Remove DAG callback-only hooks. Other default execution,
  result, provider and persisted V4/V6/V9 contracts are preserved; V1 batch reads
  remain supported. See [migration](migration.md#unreleased).

Verification covers actual Chat Completions/Responses request equivalence,
public result and continuation contracts, review
recovery, retry/compaction, failure boundaries, slow consumers and memory release.
Run the Python test suite and offline example listed in
[the persistence guide](conversation-changes.md).

Limits: authoritative final messages still accompany display deltas; review V9
checkpoints retain full state and duplicated fields; there is no chunking or hard
frame bound. Unlimited ordinary retention and replay-ID growth remain explicit.
This entry documents source changes; it does not indicate package publication.
