# Persist ToolLoop conversations from the stream

**Unreleased — batch contract V2.** Enable persistence with
`StreamPersistenceOptions` on `Runner.stream` or `resume_stream`. The former
`on_conversation_change` callback and its error types have been removed; see
[the V1 to V2 migration](migration.md#unreleased). Without persistence options,
the existing complete-result stream and diagnostic trace remain unchanged.

This mode supports `ToolAgent`, registered tool-agent delegation, and the tool
branch of `AutoAgent`. DAG targets are rejected. Auto routing to a DAG also fails
explicitly before DAG execution. DAG execution, events and recovery are otherwise
unchanged; its former callback-only hooks have been removed.

The host's committed batch journal is authoritative. `ConversationState` is the
materialized model context; a review `RunCheckpoint` is a portable continuation.
The SDK owns neither a database nor host permissions, transactions or file storage.

## Read, commit, acknowledge

```python
import dagent

options = dagent.StreamPersistenceOptions(
    max_buffer_events=256,
    max_buffer_bytes=1_048_576,
    ack_timeout_seconds=None,
)

async for event in runner.stream(
    agent, input="Continue the investigation.", conversation=previous_state,
    persistence=options,
):
    if event.type == "conversation.changed":
        batch = event.data
        await host.commit_batch(batch)  # application transaction + required files
        await runner.acknowledge_changes(batch.run_id, batch.batch_id)
    elif event.type == "response.content.delta":
        display(event.data.delta)
    elif event.type == "run.failed":
        handle_delivery_failure(event.data)
```

This short example serializes reads and writes. For continued display while the
host writes, run a reader and a single bounded writer task. The complete offline
[example](../../examples/conversation_changes.py) demonstrates that arrangement,
JSONL replay, cross-Runner review recovery, files and moved workspaces:

```bash
uv run python -m examples.conversation_changes
```

Only one batch can await confirmation per delivery. The SDK confirms inputs and
response starts before model dispatch, accepted replies before tool dispatch,
tool starts before execution, and results before further dispatch. Tokens do not
require individual acknowledgements. The final batch is also acknowledged.

If committing fails, call `reject_changes(run_id, batch_id, message=...)`.
Rejection and timeout stop subsequent dispatch; tools are never automatically
replayed. Acknowledging the latest confirmed batch again is safe during the
active delivery. Wrong, stale or closed-delivery acknowledgements return `False`.
A commit can succeed before its ACK is lost: deduplicate using batch identity.

`ack_timeout_seconds` starts when the consumer receives the batch. `None` imposes
no persistence timeout. Buffer limits bound the ready queue, plus a bounded
producer emission group. A single oversized event may occupy the queue alone.
They are not frame-size limits. Synchronous producers that overrun their bounded
emission group fail explicitly; ordinary SDK production awaits buffer capacity.

## Public batch contract

`RunStreamEvent.type == "conversation.changed"` carries a
`ConversationChangeBatch` directly. New runs publish V2; existing V1 journal
records remain readable with the same public model.

| Fields | Meaning |
| --- | --- |
| `delivery_id`, `sequence`, `batch_id` | One delivery per run/resume, ordered from sequence 1; identity supports host deduplication |
| `run_id`, `conversation_id` | Execution and public conversation identity |
| `base_revision`, `revision` | Context revision before/after the batch; revisions can jump or remain equal |
| `conversation_schema_version` | Public conversation format, currently 4 |
| `messages` | Full typed messages, source attribution, response identity and partial marker |
| `context` | Ordered membership, message updates, deactivation, summary and compaction-state changes |
| `observations` | Response and tool lifecycle facts, including errors and uncertain execution |
| `resources` | Public file references and their location inputs |
| `status` | Absent for ordinary batches; `completed`, `awaiting_review`, `failed` or `cancelled` ends this delivery |
| `usage`, `context_usage` | Terminal cumulative operation counts and this run's context usage |
| `error`, `workspace_path` | Public `RunError` and workspace when available |
| `output_item_id`, `output_text` | Mutually exclusive final output reference or fallback text |
| `checkpoint` | Existing V9 checkpoint, present only for `awaiting_review`, without trace |

Resume retains `run_id` and creates a new `delivery_id`. Event sequence numbers
and batch sequence numbers are independent. Observation-only batches need not
advance the conversation revision. Cumulative usage after review must replace the
run's prior total, rather than being added again.

Tool observations include `assistant_message_id`: correlate tools by assistant
message and call ID, since providers can reuse call IDs in later rounds. A known
completed tool whose output storage failed remains completed; retention records
report unavailable data, and the terminal run error reports the storage failure.
A cancelled tool can have `outcome_unknown`, without an invented result message.

## Reconstruct the next context

Append batches to the durable journal once. A latest-message index is a projection,
not a substitute for message versions and the journal. Apply each batch atomically:

1. Verify batch order, identity and `base_revision`; save messages and resources.
2. Save observations; apply membership, updates, summary and compaction changes.
3. Save terminal information and any checkpoint; commit, then ACK.

Message keys are `(origin.id, item.id)`. Public membership refers to messages with
`origin.id == conversation_id`. Validator, compactor, router and subagent records
are audit sources; neither their presence nor `partial=True` implies membership.
For summary replacement, remove `replaced_item_ids`; a replacement value of `None`
clears the summary or compaction state. Keep retained item order and apply added
placements in order. Updated and reactivated records may require the host index.

Construct the existing public input yourself:

```python
state = dagent.ConversationState(
    id=conversation_id, revision=revision, items=tuple(active_messages),
    summary=summary, compaction_state=compaction_state,
)
```

Store fields verbatim: reasoning, refusal, usage and model metadata, tool IDs and
arguments, frozen result displays, retention, references and compaction state all
retain their existing meanings. The SDK continues to assemble provider requests.
Persistence does not change prompt configuration, tools, replay, compaction policy,
result-file thresholds or provider argument precedence. Host-rebuilt context is
checked against actual Chat Completions and Responses request payloads.

For normal output, resolve `output_item_id` and read
`item.content.strip() or item.refusal.strip()`. Use `output_text` only when present.
Display deltas remain provisional: retry-discarded text is not a formal reply.

## Interruption and review

An ordinary next turn or retry uses a new run ID and reconstructed conversation.
Use `input=None` to retry the user request already in that context without adding
it twice; use a new input for a new question or new uploads. `input=None` reuses
saved attachments and rejects new `input_uploads`. AutoAgent retries continue its
ToolLoop without routing again. Before model dispatch, persistent
ToolLoop removes an unfinished tail assistant/tool group from effective context
and publishes that deactivation for ACK. Earlier closed turns and user messages
remain; original messages and known/unknown execution facts remain in the journal.
This does not undo side effects or automatically replay a tool. Malformed orphan
results are rejected rather than guessed.

Review continues the same run and needs the existing public checkpoint:

```python
checkpoint = dagent.RunCheckpoint.model_validate_json(saved_checkpoint_json)
runner.validate_checkpoint(checkpoint)  # read only; does not consume review
calls = checkpoint.pending_tool_invocations
stream = runner.resume_stream(
    checkpoint.review.approve(), checkpoint=checkpoint, persistence=options,
)
```

Finish the previous stream delivery before resuming. An overlapping active run
is rejected before consuming the review decision. The host stores the checkpoint
whole; it never constructs private pending batches.
`pending_tool_invocations` returns detached, remaining potentially executable calls.
Checkpoint validation checks structure, plan fingerprints and available registered
capabilities/skills. Host policy checks remain host responsibilities.
After the review terminal batch is acknowledged, local heavy state and checkpoint
are released, even with unlimited ordinary retention. Resume into a fresh Runner
with compatible registered capabilities. In-process replay tombstones remain;
cross-worker review deduplication belongs to the host transaction.

## Size, memory and failures

Persistent mode constructs no ToolLoop trace, including delegated ToolLoops. It
suppresses trace snapshots, duplicate capability bodies, duplicate review events
and full `run.finished` results. Normal terminal batches have no conversation,
model thread, history collection or checkpoint. There is no `RunSummary` model.
Display deltas and each authoritative final message still cross the stream once
each. Real message updates may publish new versions. Review checkpoints retain
their existing complete V9 representation and duplicated fields.

Configure `Runner(max_retained_runs=0)` when the host owns history. Ordinary
terminal caches still follow the existing setting (`None` remains unlimited);
completed persistent runs have no cached checkpoint. Active context, current
response, one pending batch and bounded queues remain necessary. Used run/review
IDs continue to grow; this is not an assertion of constant total process memory.

Execution failures arrive as acknowledged terminal `status="failed"` batches.
Delivery failures arrive as `run.failed` with message/error type plus delivery,
failed-batch and last-confirmed sequence/revision, without a full result or failed
batch replay. The SDK cannot claim an unacknowledged terminal was stored.

Persistent `cancel()` accepts a stop request without waiting for host storage.
If an ordinary batch is awaiting ACK, confirmation first advances the conversation
baseline, then cancellation publishes a `cancelled` terminal based on that revision.
The cancellation completes when that terminal is acknowledged. Once any terminal
batch starts delivery, `cancel()` returns `False`: execution has ended and the
existing terminal still requires ACK, rejection or timeout. Closing the stream
releases ACK waits and cancels work, including terminal delivery. Review state is
released when its terminal is confirmed or delivery is abandoned. A disconnected
host uses its committed journal to decide interruption status.

Resources contain references, not bytes. Use `Runner.resolve_runtime_path` to
read them and preserve required bytes before ACK. Restore logical paths yourself
when moving workspaces; the SDK does not copy or scan storage. Mutable upload
attachments and immutable result/compaction archives retain distinct semantics.
Upload helpers are public in `dagent.artifacts`; no runtime-private imports are
needed for this ToolLoop integration.
