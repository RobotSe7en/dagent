# Release notes

## Unreleased — ToolLoop persistent stream V2

- Add opt-in `StreamPersistenceOptions` to `stream`/`resume_stream`, one mixed
  event stream, explicit ACK/rejection and bounded delivery buffers.
- Extend existing `ConversationChangeBatch` with terminal fields and the existing
  review checkpoint. No `RunSummary` or second checkpoint model is introduced.
- Execute ToolLoop and delegated tool agents without constructing diagnostic trace.
  Persist response/tool errors, usage, uncertain execution and resource references.
- Add public checkpoint review, detached pending-tool invocation and read-only
  validation APIs, plus public upload helpers in `dagent.artifacts`.
- Allow host-rebuilt context continuation and opt-in `input=None` retry; deactivate
  unfinished tail tool groups without fabricating results or replaying effects.
- Release acknowledged review state; ordinary state retention remains explicit.
- **Breaking, explicitly authorized:** remove `on_conversation_change` from all
  four execution APIs, its Handler/Failure/Error contracts and the failure-event
  callback field. Remove DAG callback-only hooks. Other default execution,
  result, provider and persisted V4/V6/V9 contracts are preserved; V1 batch reads
  remain supported. See [migration](migration.md#unreleased).

Verification covers actual Chat Completions/Responses request equivalence,
normal API snapshots captured from the unmodified pre-change source, review
recovery, retry/compaction, failure boundaries, slow consumers and memory release.
Run the Python test suite and offline example/benchmarks listed in
[the persistence guide](conversation-changes.md).

Limits: authoritative final messages still accompany display deltas; review V9
checkpoints retain full state and duplicated fields; there is no chunking or hard
frame bound. Unlimited ordinary retention and replay-ID growth remain explicit.
This entry documents source changes; it does not indicate package publication.
