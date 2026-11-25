# Wait for user input inside a tool

**Available since 0.11.1, opt-in.** User interactions retain the original local
root ToolLoop, execution coroutine, invocation, capability scope, model, and step
budget. No Enterprise service, UI framework, HTTP transport, database, or live
model is required. Run the complete offline host example:

```bash
uv run python -m examples.user_interactions
```

## Enable a receiver for one execution

```python
import asyncio
import dagent

requests: asyncio.Queue[dagent.UserInteractionRequest] = asyncio.Queue()
options = dagent.UserInteractionOptions(
    on_request=requests.put_nowait,
    on_end=lambda event: print(event.reason, event.invalidated),
    timeout_seconds=None,
)
agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=[*dagent.ui_tools()],
)
task = asyncio.create_task(runner.run(agent, input="Ask me", interactions=options))

request = await requests.get()  # e.g. a confirm request
await runner.respond_to_interaction(dagent.UserInteractionAnswer(
    run_id=request.run_id,
    invocation_id=request.invocation_id,
    interaction_id=request.interaction_id,
    value=False,
))
result = await task
```

`runner` above is an existing configured Runner. A host should continuously
consume its request queue and handle each requested type; the offline example
implements that loop. Both notification callbacks are synchronous and must return
promptly. Schedule asynchronous host work separately. Submit responses on the
owning Runner's execution event loop; a multi-worker host routes requests to the
worker and Runner that own the run.

`interactions` is optional on `run`, `stream`, `resume`, and `resume_stream`.
There is no Runner default or implicit receiver inheritance. An independent run
started inside a tool, including a delegated tool, must supply its own options.
Review continuation also supplies fresh options; receivers are not serialized
into checkpoints.

Only local root ToolLoops support interaction. This includes an AutoAgent that
routes to tools. Explicit interaction configuration for DAG or sandbox execution
is rejected before execution; AutoAgent's DAG branch is rejected before entering
the DAG. Resuming a sandbox checkpoint with interactions is rejected before
consuming the review, even when `execution` is omitted. A delegated `agent.*` never
inherits interaction permission. Attempts inside unsupported tool contexts raise
`UserInteractionUnsupported` and follow existing tool-failure handling without
creating a wait.

## Four interaction types

`ui_tools()` returns separately registerable bindings with stable IDs
`tool.ui_confirm`, `tool.ui_select`, `tool.ui_input`, and `tool.ui_editor`.
They use the existing catalog, tool selection, disablement, and execution rules.
They are not added to the default toolset.

Starting in 0.11.2, the model-facing tool definitions describe each parameter
and the returned value. `tool.ui_editor` takes the complete initial content and
returns the text the user submits in the interactive editor.

| Context operation | Request parameters | Normal value |
| --- | --- | --- |
| `await context.ui.confirm(title, description=None)` | Title and optional description | `bool` |
| `await context.ui.select(title, options)` | Title and nonempty options with unique string `id` and `label` | Selected ID as `str` |
| `await context.ui.input(title, prompt="", initial="")` | Title, prompt, initial text | Complete submitted `str` |
| `await context.ui.editor(title, content, format="text")` | Title, original content, `text/markdown/html` | Complete submitted `str` |

`UserInteractionOption(id="draft", label="Draft")` defines an option. The SDK
validates actual response types and select membership. `False` and `""` are
normal values. HTML rendering, editing, and serialization belong to the host;
the SDK forwards the full string without transforming it.

Custom asynchronous tools can use the public context without registering any
model interaction tools:

```python
@dagent.tool(supports_context=True)
async def draft(*, context: dagent.ToolContext, callbacks=None) -> str:
    prefix = "Author: "
    name = await context.ui.input("Name", initial="Ada")
    try:
        await context.ui.confirm("Include appendix?")
    except dagent.UserInteractionCancelled:
        return prefix + name
    return prefix + name + "\nAppendix included."
```

The existing `context=` and `callbacks=` injection remains unchanged. The tool's
locals survive the wait. Disabling a built-in model tool restricts direct model
calls to that tool; the run receiver and supported execution scope independently
control custom `context.ui` calls. Context handles expire when the invocation
ends and cannot be reused by escaped background work.

## Respond, abandon, or stop

Every request carries protocol version 1, SDK-generated `interaction_id`, and
runtime-bound `run_id`, `invocation_id`, `assistant_message_id`, and
`capability_id`. Model tool arguments cannot set those identities. Existing
invocation IDs retain their provider call ID; use the assistant message identity
as well when associating calls across rounds that reuse a call ID. Request and
response models live in `dagent.schemas.user_interactions`; the main public
contracts are also exported from `dagent`.

`UserInteractionRequest` is a discriminated union with typed `parameters`.
`UserInteractionResponse` is the union of `UserInteractionAnswer` and
`UserInteractionAbandonment`. Hosts can use Pydantic `TypeAdapter` to decode the
union from their transport, then submit the resulting typed object.

To abandon only the current interaction:

```python
await runner.respond_to_interaction(dagent.UserInteractionAbandonment(
    run_id=request.run_id,
    invocation_id=request.invocation_id,
    interaction_id=request.interaction_id,
))
```

Custom tools receive `UserInteractionCancelled`, distinct from run cancellation.
Built-in tools return a completed tool result with value `{"status": "cancelled"}`
and an explicit cancellation marker, so the model can continue.

To terminate the affected run:

```python
await runner.abort_interaction(
    run_id=request.run_id,
    invocation_id=request.invocation_id,
    interaction_id=request.interaction_id,
    reason="unavailable",  # or "timeout"
    message="The host editor is no longer available.",
)
```

`timeout` fails with `UserInteractionTimeout`; `unavailable` fails with
`UserInteractionUnavailable`. A receiver notification failure fails with
`UserInteractionDeliveryError`. These fatal errors cross normal tool-exception
handling and prevent subsequent dispatch. Direct calls raise the typed error
with its failed `result`; streams preserve existing failure delivery conventions.
No resumable interaction checkpoint is created.

Fatal errors stop only the run that owns the interaction. If a tool starts an
independent run and lets its finalized interaction error escape, the calling run
records an ordinary failed tool result. The error retains the inner run's `result`.

`UserInteractionResponseRejected.code` is `not_pending`, `identity_mismatch`, or
`invalid_value`. Unknown, mismatched, duplicated, expired, and late responses are
rejected. Invalid responses do not consume a valid pending request. Same-process
matching and single consumption do not provide persistent HTTP retry idempotency.

## Waiting, deadlines, and persistence

Each run has at most one pending interaction; a concurrent second attempt raises
`UserInteractionBusy`. The existing sequential tool batch waits at the current
invocation. No later tool or model runs until it finishes, and completed tools
are not replayed. Independent runs remain isolated. With opt-in
[parallel tools](parallel-tools.md), UI calls remain exclusive barriers. Custom
interactive tools must keep `parallel_safe=False`; a parallel-dispatched tool
requesting UI gets `UserInteractionUnsupported` without opening a request.

The monotonic deadline starts when the request is registered, immediately before
notifications. `timeout_seconds=None` sets no SDK deadline; a host can pass its
chosen value, such as `1800`, or maintain its own timer and call
`abort_interaction(reason="timeout")`. Thirty minutes is not an SDK default.
Interaction timeouts and persistence ACK timeouts are separate mechanisms.

Waiting rejects new `Runner.steer` calls with `RunNotSteerableError`. Previously
accepted steering remains queued for the existing execution boundary.
If a custom tool cancels only its UI await (for example, by catching a timeout
from `asyncio.wait_for`) and continues, steering becomes available again and
queued steering is retained. This differs from the fatal SDK interaction timeout.
`Runner.cancel` retains the entrypoint's existing cancellation behavior. Stopping,
closing the stream or Runner, or unwinding an invocation invalidates pending
requests and releases waiters and timers. Runner close synchronously invokes the
end callback even when the stream is no longer being consumed.

`user_interaction.requested` and `user_interaction.ended` are typed stream events.
The end record includes identity, reason, and `invalidated`. Reasons include
`submitted`, `cancelled`, `timeout`, `unavailable`, `run_cancelled`, `runner_closed`,
`invocation_ended`, and `delivery_error`. Hosts can use callbacks or these events
to manage separate user-wait and ordinary progress clocks. Lifecycle notifications
are transient; waiting does not introduce a durable Run status.

Built-in user values become the original invocation's tool result. Custom tools
return their own final result. Both paths use existing storage, long-content
externalization, `ContentReference`, and model display/recovery rules. With
`StreamPersistenceOptions`, the result batch must be saved and ACKed before the
next tool or model dispatch. Response acceptance and a wait-ended notification
do not mean the result was saved.

Associate the request's `assistant_message_id` and `invocation_id` with the
existing `tool_execution` observation's `assistant_message_id` and `call_id`;
`result_item_id` locates the result message. Save required resources before ACK.
Hosts never assemble provider messages or fabricate tool results. See
[Conversation Changes](conversation-changes.md) and
[Tool Result Recovery](tool-result-recovery.md).
