"""Display-order gating for the chat SSE endpoints.

``/messages/stream`` and ``/messages/resume`` are backend-for-frontend
endpoints for the chat UI: this module filters and reorders the public SDK
event stream so the client can render events in display order without
mode-specific logic. ``Runner.stream`` itself stays chronological and
untouched.

Policies, decided by the run kind carried in the leading ``run.started``
event:

- non-``tool`` runs (dynamic DAG): ``response.content.delta`` is dropped.
  The planner/node token streams were never rendered by the chat UI; the
  final answer reaches the client through ``run.finished``'s ``output_text``,
  which already arrives after any validation events.
- ``tool`` runs with validation enabled: content deltas and the closing
  ``response.finished`` of each model call are held back per ``response_id``.
  A held response is released as soon as a later event proves it was an
  intermediate step (the next ``response.started`` or a capability event),
  so the loop's final answer is the only response still held when the loop
  ends: ``validation.passed`` releases it right after the verdict, while
  ``validation.retry`` keeps it back so the rejected draft never renders.
  A rejected draft is discarded only once the retry attempt actually starts
  streaming; if the run finishes first (retries exhausted — the runtime
  returns the unvalidated answer as the result), ``run.finished`` releases
  it instead.
- ``tool`` runs with validation disabled: passthrough.

Sequence numbers are preserved, so released events keep their original
(chronological) sequence even though they are emitted later.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from dagent import RunStreamEvent


_HELD_EVENT_TYPES = frozenset({"response.content.delta", "response.finished"})
_PASSTHROUGH_EVENT_TYPES = frozenset({"response.reasoning.delta", "validation.started"})


class _ChatDisplayGate:
    """Sequential state machine over one run's event stream."""

    def __init__(self, *, validation_enabled: bool) -> None:
        self.validation_enabled = validation_enabled
        self.kind: str | None = None
        self._held: dict[str, list[RunStreamEvent]] = {}
        self._rejected = False

    def process(self, event: RunStreamEvent) -> list[RunStreamEvent]:
        if event.type == "run.started":
            self.kind = getattr(event.data, "kind", None)
            return [event]
        if self.kind != "tool":
            if event.type == "response.content.delta":
                return []
            return [event]
        if not self.validation_enabled:
            return [event]
        if event.type in _HELD_EVENT_TYPES:
            response_id = str(getattr(event.data, "response_id", "") or "")
            self._held.setdefault(response_id, []).append(event)
            return []
        if event.type in _PASSTHROUGH_EVENT_TYPES:
            return [event]
        if event.type == "validation.passed":
            return [event, *self._release()]
        if event.type == "validation.retry":
            self._rejected = True
            return [event]
        if event.type in {"run.finished", "run.failed"}:
            return [*self._release(), event]
        # Anything else (the next response.started, capability events, ...)
        # proves the held responses were intermediate steps — or, after a
        # rejection, that the retry attempt is underway and the draft can be
        # dropped.
        if self._rejected:
            self._discard()
            return [event]
        return [*self._release(), event]

    def drain(self) -> list[RunStreamEvent]:
        return self._release()

    def _release(self) -> list[RunStreamEvent]:
        released = [event for events in self._held.values() for event in events]
        self._held.clear()
        self._rejected = False
        return released

    def _discard(self) -> None:
        self._held.clear()
        self._rejected = False


async def gate_chat_display(
    events: AsyncIterator[RunStreamEvent],
    *,
    validation_enabled: bool,
) -> AsyncIterator[RunStreamEvent]:
    """Yield ``events`` filtered/reordered for chat display (module docstring)."""
    gate = _ChatDisplayGate(validation_enabled=validation_enabled)
    try:
        async for event in events:
            for emitted in gate.process(event):
                yield emitted
    except Exception:
        for emitted in gate.drain():
            yield emitted
        raise
    for emitted in gate.drain():
        yield emitted
