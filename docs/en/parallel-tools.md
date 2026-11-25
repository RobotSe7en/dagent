# Run tools in parallel

**Available since 0.11.3.** ToolLoop can overlap independent tool calls from one model reply.
Existing applications remain sequential: `max_tool_concurrency` defaults to `1`.
The model must return multiple calls for this to help; this setting does not
change provider request fields or force the model to call multiple tools.

## Enable parallel tools

```python
import dagent


@dagent.tool(parallel_safe=True)
async def lookup(query: str) -> dict[str, str]:
    return await search_service.lookup(query)


agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=[lookup],
    max_tool_concurrency=4,
)
result = await runner.run(agent, input="Look up the three topics.")
```

`max_tool_concurrency` accepts positive integers, excluding booleans. `1` keeps
sequential execution; larger values enable a rolling pool. When one call finishes,
another can start even while an earlier call is still running. The limit belongs
to each tool loop; independently running agents have separate limits.
`AutoAgent` accepts the same setting for its tool route. Registered `ToolAgent`s,
including static DAG Agent nodes, use their own setting for their internal tools.
The graph scheduler and the parent's Agent delegation calls keep their existing
behavior.

Run the credential-free example:

```bash
uv run python -m examples.parallel_tools
```

## Declare which tools can overlap

`@tool(parallel_safe=True)` promises that the handler can run alongside other
parallel-safe handlers. Shared clients and mutable state must support concurrent
calls. The handler must return its result through the SDK and must not mutate
the parent conversation or request user interaction. The declaration is optional
and defaults to `False`; raw registrations use `CapabilityDefinition.parallel_safe`.
`ToolRegistry.register` also accepts `parallel_safe` for registry-owned tools.

The built-in `read_file`, `list_files` and `grep` tools opt in. Writes, edits,
shell, UI tools, Agent delegation, and other undeclared capabilities are
exclusive. A loop waits for earlier calls before executing an exclusive call,
then finishes that call before starting later calls. For example:

```text
read(A), read(B), write(A), read(C)
     overlap       alone    afterwards
```

Risk and review requirements are independent of parallel safety. Approved path
boundary overrides are also exclusive because they update the run's boundary.

Async handlers run on the event loop. Parallel-dispatched synchronous Python
tools run in threads, including synchronous functions wrapped with `@tool`.
Context variables and `supports_context=True` are preserved. Sequential
decorated functions keep their existing execution location. This primarily
benefits I/O workloads; a thread does not guarantee multicore speedup for pure
Python CPU work.

Progress callbacks (`callbacks.on_token` and `callbacks.on_event`) from threaded
parallel tools are delivered on the execution event loop. Ordinary and persistent
streams can receive progress while the tool is still running. Cancellation drops
callbacks that have not been delivered, including those already queued by a worker.

## Enable an MCP connection

The MCP **server** declares safety on each tool returned by `tools/list`:

```json
{
  "name": "search",
  "inputSchema": {"type": "object"},
  "_meta": {"dagent/parallelSafe": true}
}
```

This is a dagent extension, not a standard MCP annotation. Missing or `false`
means exclusive. Non-boolean values fail registration. `readOnlyHint`, risk,
destructiveness and idempotence do not imply parallel safety.

Set the host-owned connection capacity separately:

```python
runner.add_mcp_server("docs", {
    "command": "python",
    "args": ["docs_server.py"],
    "max_concurrency": 4,
})
agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["mcp.docs.search"],
    max_tool_concurrency=4,
)
```

`max_concurrency` defaults to `1` and accepts positive integers. The connection
shares this capacity across Runs. Undeclared tools acquire exclusive access to
the connection; their waiting position also prevents later parallel calls from
overtaking them. Cancellation and timeouts release admission capacity. Both stdio
and Streamable HTTP use the same scheduler. MCP snapshots preserve the tool's
declaration for lazy registration and reload.

## Results, review and interruption

Completion events (`capability.call.completed` / `capability.call.failed`) and
persistent `tool_execution` observations arrive as tools finish. Conversation
results and persisted tool messages retain the model's original order. A fast
tool's completion observation can therefore arrive before its result message;
`result_item_id` is optional. The next model request waits for the round to finish.

Whole-round review still completes before tool dispatch. A fresh boundary review
stops dispatch, drains started calls and commits the completed prefix before
saving a checkpoint. Resuming never replays that prefix. Parallel plans freeze
the concurrency setting and safety declarations, including registered child
tools; changed declarations fail resume validation. Released checkpoints without
these fields retain sequential behavior and their existing fingerprints.

Persistent execution still waits for each tool's starting observation to be
acknowledged before executing that tool. Conversation revisions and publications
remain serialized. See [Persistence](conversation-changes.md).

An ordinary tool error becomes its failed result and does not cancel siblings.
Steering stops new dispatches, lets started calls finish, marks undispatched calls
skipped and then applies the new instruction, even if every call had already
started. If no model steps remain, the instruction is explicitly discarded with
`step_limit_exhausted` and the run fails. Cancellation, persistence rejection
and required result-storage failures stop dispatch and cancel outstanding tasks.
Interrupted calls without a confirmed result use `outcome_unknown`; rejected
persistence cannot guarantee those observations were saved. Python threads and
remote effects cannot be forcibly rolled back, and such calls are not replayed.

UI tools run exclusively. A parallel-dispatched custom tool calling `context.ui`
gets `UserInteractionUnsupported` without opening an interaction. Keep custom
interactive tools at the default `parallel_safe=False`. See
[User Interactions](user-interactions.md).
