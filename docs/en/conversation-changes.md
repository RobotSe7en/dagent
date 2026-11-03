# Persist conversation changes incrementally

Available in the **Unreleased** SDK. The optional `on_conversation_change`
callback is shared by `Runner.run`, `stream`, `resume`, and `resume_stream`.
Omit it to retain existing behavior, event serialization, provider requests,
compaction policies, and result/checkpoint formats.

The SDK publishes its adopted state changes. The host saves them and later
constructs the existing `ConversationState`; it does not reproduce compaction or
context assembly. The SDK does not manage a database, permissions, transactions,
or a host file store.

## Subscribe and acknowledge

```python
import dagent


async def save_changes(batch: dagent.ConversationChangeBatch) -> None:
    await host.save_batch(batch)  # Return only after the host commits.


result = await runner.run(
    agent,
    input="Continue the investigation.",
    conversation=previous_state,
    on_conversation_change=save_changes,
)
```

`host.save_batch` above is application code. A callback must return an awaitable.
The SDK waits for it before advancing to the next model or tool operation.
Text and reasoning continue through the existing stream events; callbacks do not
receive a complete message for every token. The host may use a timeout inside
its callback. The SDK imposes no persistence timeout or automatic retry. Saving ready resources
does not consume the compactor's timeout allowance.

Use the same keyword on `stream`, `resume`, and `resume_stream`. Review resumes
still require their separate `RunCheckpoint`; conversation changes do not
authorize execution from arbitrary interruption points.

Run the offline example:

```bash
uv run python -m examples.conversation_changes
```

It writes a host JSONL journal, copies notified files, saves an ordinary review
checkpoint, resumes it, reloads the journal, and continues from reconstructed
state without storing `result.conversation`.

## Batch order and identity

`ConversationChangeBatch` V1 contains:

| Fields | Meaning |
| --- | --- |
| `delivery_id`, `sequence`, `batch_id` | One delivery session per run/resume invocation; sequence starts at 1; batch identity is stable for host deduplication |
| `run_id`, `conversation_id` | Runtime run and public conversation identity |
| `base_revision`, `revision` | SDK revision before and after this batch; revisions can jump |
| `conversation_schema_version` | Version of the reconstructed public state, currently 4 |
| `messages` | Complete typed message records or explicitly partial observations |
| `context` | Effective public membership, summary, and compaction-state changes |
| `observations` | Response lifecycle and factual tool-execution observations |
| `resources` | Ready typed file references and their physical-location inputs |

Resume retains the run ID but creates a new delivery ID. Stream event sequence
numbers are independent of batch sequence numbers. Audit/resource-only batches
keep the same conversation revision. A static DAG without a public conversation
has `conversation_id`, state version, and both revisions set to `None`, and no
public `context` changes; its internal audit/resources can still be saved.

Apply batches in delivery order. Check `base_revision` against the saved state
and store the batch receipt with the host transaction. The SDK reports an
acknowledgment only when the callback returns successfully. A host commit can
succeed before a later callback error, so failed delivery does not prove the
batch was absent from storage. Hosts coordinate concurrent writes themselves.

## Save messages and effective membership separately

Store message records by `(origin.id, item.id)`. The record contains the full
`UserMessage`, `AssistantMessage`, or `ToolResultMessage`, plus source attribution,
an optional `response_id`, and a `partial` marker. Public membership uses records
whose `origin.id` equals the public conversation ID. Planner, validator, and
subagent audit records are separate sources, not implicit public history.

Apply a non-null `context` as one atomic change:

1. Upsert `messages` into the host's structured-record table.
2. Remove `deactivated_item_ids` and, when a summary changes, its
   `summary_change.replaced_item_ids` from the effective ordered items.
3. Replace the effective items listed by `updated_item_ids` from their complete
   public records; their positions stay unchanged.
4. Apply `added` placements in order, using their item IDs and zero-based indexes
   in the list after the preceding changes.
5. Apply the full summary/compaction-state replacements and the batch revision.

Removing membership does not delete the audit record. Summary replacement and
the exact messages it replaces are delivered in the same batch. Internal
compaction candidates that are later rejected do not remove public messages.
Summary tightening can replace the previous summary without removing any items.

`summary_change=None` means unchanged; `summary_change.value=None` explicitly
clears the summary. `compaction_state_change` uses the same distinction.
Revision-only changes are valid. Result-display updates include the complete
message, including frozen display/recovery metadata, retention, and feedback.
Unchanged historical messages are not republished.

Reconstruct the state from the saved effective records:

```python
previous_state = dagent.ConversationState(
    id=conversation_id,
    revision=saved_revision,
    items=tuple(saved_active_items),
    summary=saved_summary,
    compaction_state=saved_compaction_state,
)
```

Supply it through the existing `conversation=` argument. Keep the initial
conversation and its existing resources when enabling delivery on an established
conversation. System prompts are generated separately and are not history.
Internal audit is not a replacement for an internal model-thread checkpoint.

## Correlate streams and preserve interruption evidence

When enabled, response start, content/reasoning delta, and finish events carry
the SDK `message_id` allocated before generation. Batch response observations
and the complete structured message use that identity and `response_id`.
Tool calls retain their own IDs; tool results link through `call_id`.

On cancellation or unexpected stream failure, observed content/reasoning is
published once as a partial message with a terminal observation. Effective
membership is determined exclusively by `context`, including the SDK's existing
failure behavior. A partial observation must not be appended automatically.
If a transport error's SDK message omits text already streamed, terminal
observations preserve the differing `observed_content` / `observed_reasoning`.
Save observations alongside message records; do not overwrite the adopted
message with these display/audit fields.

Tool `starting` records a saved execution intention before dispatch, not proof
of an effect. `completed` and `failed` report known outcomes. If dispatch was
interrupted without a returned result, `outcome_unknown` preserves that
uncertainty. No result is invented and tools are never automatically replayed.

Cancellation stops further advancement and waits for the in-flight callback and
necessary cleanup deliveries. It may therefore wait on host persistence. This
does not promise per-token durability after a process crash or undo tool effects.

## Save referenced files

Each ready resource includes a complete `ContentReference` or `Attachment`, its
`workspace_path`, and the actual `runtime_directory` used for that resource.
Writes have completed before delivery. Locate the file using the public SDK:

```python
for resource in batch.resources:
    path = runner.resolve_runtime_path(
        resource.reference.path,
        workspace_path=resource.workspace_path,
        runtime_directory=resource.runtime_directory,
    )
    await host.save_file(path, resource.reference)
```

Save required file bytes before acknowledging references. SDK-created result
files, received/display snapshots, compaction sources/records, and newly
materialized references are notified without scanning directories. References
remain logical even with an absolute external runtime directory. Existing SDK
backing-store restoration remains responsible for cross-workspace continuation;
this feature does not add a host file-restoration API or relocate existing data.
Mutable attachments retain their existing upload-time metadata semantics.

## Handle persistence failures

`run` and `resume` raise `dagent.ConversationChangeError`, a `RunExecutionError`
subclass. It exposes `failed_batch`, `acknowledged_sequence`,
`acknowledged_revision`, `failure`, and an auditable failed `result`; `__cause__`
holds the original host error. The result has no resumable checkpoint.

`stream` and `resume_stream` emit one `run.failed` with
`error_type="ConversationChangeError"`, `data.result`, and the equivalent typed
`data.conversation_change_failure`. The SDK stops further work, including
quality-validation retries. A completed tool remains completed in execution
evidence even though the Run failed to persist.

The new callback is disabled by default. `result.conversation`, `new_items`,
review checkpoints, and full `run.finished` results remain supported. The
incremental-size guarantee concerns the new notifications, not the existing full
terminal result. Filter frontend transport in the host when necessary.
