# Non-Executing DAG Design

Use `Runner.design_dag(...)` when an application needs a typed DAG candidate
before it decides whether or where to save or run that design. A design turn can
create a DAG from natural language, revise a complete existing `DAGSpec`, report
that no change is needed, or answer a question about the graph.

This API is deliberately separate from execution. It calls the configured chat
provider once, but it does not create a Run, `PendingReview`, checkpoint,
workspace artifact, or capability result. It never calls a capability handler.

## Create a candidate

```python
import dagent


@dagent.tool
def summarize(text: str) -> str:
    return text[:200]


runner = dagent.Runner(provider=provider, capabilities=[summarize])
result = await runner.design_dag(
    "Create a DAG that summarizes its string input.",
    agent=dagent.DagAgent(capabilities=["tool.summarize"]),
)

if isinstance(result, dagent.DAGDesignProposal):
    candidate = result.candidate  # a complete, validated DAGSpec
    print(result.summary)
elif isinstance(result, dagent.DAGDesignFailure):
    for diagnostic in result.diagnostics:
        print(diagnostic.code, diagnostic.message)
```

See [`examples/dag_design.py`](../../examples/dag_design.py) for a runnable
offline example using `MockProvider`.

The optional `agent` is an ordinary declarative `DagAgent`. Its profile,
context policy, skills, registered subagents, and `capabilities` scope are
resolved in the same way as a dynamic DAG run. When it is omitted,
`design_dag` uses the built-in non-executing `dag_design` profile and the
runner's normally visible capability catalog. Pass an explicit `DagAgent` to
select a custom profile or narrower scope. Callers do not submit copied
capability definitions; the runner catalog remains authoritative.

## Observe provider reasoning and validation

Pass a fast synchronous `on_event` callback to observe the real provider stream
using the existing public `RunStreamEvent` protocol:

```python
import asyncio


events: asyncio.Queue[dagent.RunStreamEvent] = asyncio.Queue()

result = await runner.design_dag(
    "Create a DAG that summarizes its string input.",
    on_event=events.put_nowait,
)
```

An observed call emits `response.started`, zero or more
`response.reasoning.delta` events, `response.finished`, and
`validation.started`. A valid result with no error diagnostics also emits
`validation.passed`. These events have call-local sequence numbers starting at
1 and `run_id=None`, because design does not create a run.

The provider's structured JSON is deliberately not emitted as
`response.content.delta`. Awaiting `design_dag` returns the final typed result;
its `summary` or `answer` is the natural user-visible final text. Without
`on_event`, the 0.9.4-compatible path continues to call `provider.chat` rather
than `provider.stream_chat`.

Cancel the task that is awaiting `design_dag` to cancel an in-flight design.
The SDK deterministically closes the provider stream and propagates
`CancelledError`. Provider and callback exceptions also propagate; design does
not manufacture run failure or cancellation events.

## Revise an existing DAG

Pass the authoritative current `DAGSpec` and, if useful, a neutral selection
hint:

```python
result = await runner.design_dag(
    "Change only the selected writer step to produce Markdown.",
    agent=agent,
    current=current_spec,
    selection=dagent.DAGDesignSelection(node_ids=("write_report",)),
    conversation=previous_result.conversation,
)
```

A proposal always contains the full candidate `DAGSpec`, never a JSON Patch.
For a revision, the SDK preserves the current spec id, increments its version,
keeps retained nodes and artifacts in their current order, and keeps unchanged
edges in their current order before new edges. It reuses an invocation id when
the node id, payload kind, capability id, and arguments remain unchanged.
Retained node runtime status is not rewritten by a design turn.

The model must return every retained top-level field, artifact, node, edge, and
nested DAG. This is how `input_schema`, `artifacts`, `output`, `metadata`, node
titles, edge reasons, and other public fields survive an edit. `DAGEdge` has no
SDK id field; visual edge identity, layout, semantic diff, and partial
acceptance remain host concerns.

## Result variants

`DAGDesignResult` is a discriminated union using its `type` field:

- `DAGDesignProposal` (`type="proposal"`) returns `candidate`, `summary`, and
  diagnostics.
- `DAGDesignNoChange` (`type="no_change"`) returns a short `summary` and no
  duplicate candidate.
- `DAGDesignAnswer` (`type="answer"`) returns an explanation in `answer`.
- `DAGDesignFailure` (`type="failure"`) returns typed diagnostics for invalid
  model output, an invalid candidate, or a capability outside the resolved
  catalog scope.

Every variant returns a new `ConversationState`, optional provider-reported
`ModelTokenUsage` as `usage`, and request-estimate `ContextUsage` as
`context_usage`. Input conversation objects are copied and never mutated. Pass
the returned conversation into a later turn explicitly; separate calls without
it stay isolated. The assistant item stores the natural proposal summary,
no-change summary, answer, or a short deterministic failure message. Raw
structured response JSON is never stored as visible assistant content.

## Validation and catalog authority

The model response is constrained by `StructuredOutputFormat`. A proposed
candidate then passes through four fail-closed boundaries before it is returned:

1. strict JSON decoding and DAGSpec JSON Schema validation;
2. strict Pydantic parsing into the public `DAGSpec` graph;
3. capability lookup and argument parsing against the resolved runner catalog;
4. the existing `validate_dag_spec(...)` structural and dataflow validation.

Capability `kind`, risk, and inferred boundary come from the catalog. Values in
model-authored candidate JSON cannot widen or replace them. An unknown,
disabled, or out-of-scope capability produces `DAGDesignFailure` with a stable
diagnostic code; no handler is called.

## Deterministic inspection

Use `inspect_dag_spec(...)` when no model is needed:

```python
diagnostics = dagent.inspect_dag_spec(spec)
for item in diagnostics:
    print(item.severity, item.code, item.node_id, item.path, item.message)
```

It returns a tuple of `DAGDiagnostic` values. Each item has `severity`, a stable
`code`, `message`, and, when the validator can identify them, `node_id` and
`path`. A valid spec returns an empty tuple. The function is deterministic and
does not call a provider. `validate_dag_spec(...)` keeps its existing fail-fast
exception contract unchanged.

Persistence, revisions with compare-and-swap, user or organization policy,
layout, audit, and review workflows are intentionally outside this SDK design
surface and belong to the host application.
