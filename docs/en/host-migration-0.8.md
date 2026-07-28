# Host migration for SDK 0.8

This page is an implementation specification for hosts built on dagent. The
bundled API is an example implementation; enterprise and third-party hosts
must apply these boundaries to their own persistence and authorization layers.

## Persistence boundaries

Persist two different objects:

- conversation storage: the complete bounded `ConversationState` returned by
  `RunResult.conversation`;
- pending-run storage: the complete `RunCheckpoint` returned when
  `RunResult.requires_review` is true.

Do not reconstruct either object from UI messages, traces, or database columns.
Do not persist a bare `RunState` as a resumable object.

The host remains responsible for tenant/user/project keys, authorization,
redaction, encryption, retention, and optimistic concurrency. Use
`ConversationState.id` and `revision` as the SDK-level identity and version, but
do not treat them as an authorization boundary.

## Request mapping

Map a new chat turn to:

```python
result = await runner.run(
    resolved_agent,
    input=request.input,
    conversation=stored_conversation,
    input_uploads=uploads,
)
```

Replace the stored conversation with `result.conversation` after a successful
run. Do not append `result.new_items`; the returned conversation already
contains the accepted bounded state.

Map review approval/rejection to:

```python
result = await runner.resume(
    decision,
    checkpoint=stored_checkpoint,
)
```

Consume a checkpoint exactly once. Persist a replacement checkpoint if the
resumed run reaches another review gate.
Resume also verifies the current capability definitions against fingerprints
frozen in the checkpoint. Register the same tool/MCP definitions before
restoring it; semantic changes require a new run.

## Database and API cutover

1. Add a V3 conversation document/blob column and a V3 checkpoint document/blob
   column. Preserve the whole Pydantic JSON payload.
2. Add a revision compare-and-swap when replacing conversations.
3. Change run request shapes from `messages`/`state` to `input` plus a host
   conversation identifier.
4. Change review endpoints to resolve and submit a complete V3 checkpoint.
5. Remove code that appends assistant/tool messages in the host.
6. Remove state-only resume and same-run checkpoint use from ordinary chat.
7. Reject V1/V2 records explicitly. If historical data must be retained, keep
   it read-only or migrate it in an offline, versioned job; do not add an SDK
   runtime shim.

The bundled SQLite host records a conversation schema marker. During upgrade it
marks rows without a valid, identity-matching V3 `ConversationState` as legacy
and returns HTTP 409 before reading an old `last_run_id`. New conversations are
created as V3. Other hosts should keep the same explicit distinction rather than
using `NULL` state plus revision `0` for both legacy and newly created rows.

## Reasoning and audit

`RunResult.new_items` is the full per-run audit delta and may contain
`AssistantMessage.reasoning`, internal planner/router/validator items, tool
results, and provider usage. Store it in the host's audit/event system if the
product requires full replay.

Reasoning is never part of later provider input, even though it is present in
audit items and recent conversation state. Apply host redaction and access
policy before exposing reasoning to users.

Consume these additional stream events:

- `response.reasoning.delta`;
- `context.compaction.started`;
- `context.compaction.finished`.

## Externalized results

`ContentReference.path` is workspace-relative and includes size and SHA-256
metadata. The SDK copies resources retained by a conversation into a
content-addressed store under the Runner workspace, then materializes and
rebases them for the next run workspace. Keep that Runner workspace durable.
Hosts that replace local storage must upload the referenced bytes and preserve
their typed provenance. The SDK does not create public URLs or enforce host
retention.

Never accept a client-supplied reference path as trusted. Resolve references
inside the recorded run workspace and verify their checksum.

## Rollout verification

- a two-turn conversation sends system + summary/recent history + the new turn,
  without reasoning fields;
- every assistant tool call still has one matching tool result after
  truncation/compaction;
- oversized input fails before provider invocation;
- large tool/MCP output is referenced and survives workspace upload;
- review works after process restart from only the persisted V3 checkpoint;
- duplicate or stale conversation revisions are rejected;
- V1/V2 resume attempts fail with an explicit version error.
