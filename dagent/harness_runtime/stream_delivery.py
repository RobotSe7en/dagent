"""Ordered persistent delivery with a non-blocking display lane.

Producer events and durable batches share one ordered stream, but they wait in
different places. Display events never block the producer: once the ready queue
is full they enter an overflow lane, where adjacent deltas of one response
channel merge. Only the producer's boundaries wait — every batch publication
and compaction summary request — and they wait until the consumer has drained
that overflow. Delivery failures are therefore raised only at boundaries, never
inside a model call.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace
from typing import Any
from uuid import uuid4

from dagent.result import RunStreamEvent
from dagent.schemas.conversation_changes import ConversationChangeBatch
from dagent.schemas.streaming import StreamPersistenceOptions


class PersistenceDeliveryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_DIAGNOSTIC_EVENTS = frozenset({
    "trace.updated", "dag.updated", "capability.call.started",
    "capability.call.completed", "capability.call.failed", "review.required",
})
_DELTA_EVENTS = frozenset({"response.content.delta", "response.reasoning.delta"})
_MIN_OVERFLOW_EVENTS = 32


def _text_bytes(value: Any) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, dict):
        return sum(_text_bytes(v) for v in value.values())
    if isinstance(value, (tuple, list)):
        return sum(_text_bytes(v) for v in value)
    # Avoid serializing message bodies merely to measure the buffer.
    if hasattr(value, "__dict__"):
        return _text_bytes(vars(value))
    return 0


def _merged_delta(previous: RunStreamEvent, event: RunStreamEvent) -> RunStreamEvent | None:
    """Join two adjacent deltas of one response channel; anything else stays apart."""
    if (event.type not in _DELTA_EVENTS or previous.type != event.type
            or previous.run_id != event.run_id
            or replace(previous.data, delta="") != replace(event.data, delta="")):
        return None
    return replace(previous, data=replace(previous.data, delta=previous.data.delta + event.data.delta))


class StreamDelivery:
    def __init__(self, options: StreamPersistenceOptions):
        self.options = options
        self.delivery_id = f"delivery_{uuid4().hex}"
        self.run_id: str | None = None
        self.sequence = 0
        self.acknowledged_sequence = 0
        self.acknowledged_revision: int | None = None
        self.last_acknowledged_batch: str | None = None
        self.pending: ConversationChangeBatch | None = None
        self.failure: PersistenceDeliveryError | None = None
        self.failed_batch_id: str | None = None
        self.terminal_started = False
        self.closed = False
        self.done = False
        # Ready events respect the configured limits; overflow events follow
        # them in order and are bounded by entry count and merged delta text.
        self._queue: deque[tuple[RunStreamEvent, int]] = deque()
        self._overflow: deque[RunStreamEvent] = deque()
        self._bytes = 0
        self._ready = asyncio.Event()
        self._space = asyncio.Event()
        self._ack: asyncio.Future[None] | None = None
        self._presented = asyncio.Event()

    def check(self):
        if self.failure is not None:
            raise self.failure
        if self.closed:
            raise PersistenceDeliveryError("DeliveryClosed", "Persistent stream is closed.")

    def emit(self, event: RunStreamEvent):
        """Accept a synchronous producer event without blocking or raising.

        A failed or closed delivery drops the event; its failure surfaces at the
        producer's next boundary instead of inside the code that emitted it.
        """
        if self.closed or self.failure is not None or event.type in _DIAGNOSTIC_EVENTS:
            return
        if event.type == "model.call.observed":
            event = replace(event, data=replace(event.data, model_call=None))
        self._push(event)

    def _push(self, event: RunStreamEvent) -> None:
        size = _text_bytes(event.data)
        if not self._overflow and (not self._queue or (
                len(self._queue) < self.options.max_buffer_events
                and self._bytes + size <= self.options.max_buffer_bytes)):
            self._queue.append((event, size))
            self._bytes += size
        elif self._overflow and (merged := _merged_delta(self._overflow[-1], event)) is not None:
            self._overflow[-1] = merged
        elif (event.type != "conversation.changed"
              and len(self._overflow) >= max(_MIN_OVERFLOW_EVENTS, self.options.max_buffer_events)):
            self.failure = PersistenceDeliveryError(
                "StreamBufferOverflow", "Stream events exceeded the bounded overflow between producer boundaries.",
            )
            return
        else:
            self._overflow.append(event)
        self._ready.set()

    async def _drained(self) -> None:
        while self._overflow:
            self._space.clear()
            await self._space.wait()
            self.check()

    async def _bounded_wait(self, waiter) -> None:
        timeout = self.options.stall_timeout_seconds
        try:
            if timeout is None:
                await waiter
            else:
                await asyncio.wait_for(waiter, timeout)
        except TimeoutError as exc:
            self.failure = PersistenceDeliveryError(
                "DeliveryStalled", "The stream consumer did not keep up before the stall timeout.",
            )
            raise self.failure from exc

    async def checkpoint(self):
        """Producer boundary: wait until the consumer has taken all overflow."""
        self.check()
        if self._overflow:
            await self._bounded_wait(self._drained())

    async def receive(self) -> RunStreamEvent | None:
        while not self._queue and not self._overflow:
            if self.done:
                return None
            self._ready.clear()
            await self._ready.wait()
        if self._queue:
            event, size = self._queue.popleft()
            self._bytes -= size
        else:
            event = self._overflow.popleft()
        self._space.set()
        if event.type == "conversation.changed":
            self._presented.set()
        return event

    async def publish(self, batch: ConversationChangeBatch) -> bool:
        """Confirm delivery and return whether cancellation must be propagated."""
        self.check()
        if self.pending is not None:
            raise RuntimeError("Only one persistence batch can await acknowledgement.")
        if self.terminal_started:
            raise RuntimeError("Cannot publish after a terminal persistence batch.")
        self.terminal_started = batch.status is not None
        self.pending = batch
        self._ack = asyncio.get_running_loop().create_future()
        self._presented.clear()
        try:
            self._push(RunStreamEvent(type="conversation.changed", data=batch.model_copy(deep=True), run_id=batch.run_id))

            async def wait_for_ack():
                # The batch follows every earlier event, so its presentation
                # also drains the overflow.
                await self._bounded_wait(self._presented.wait())
                if self.options.ack_timeout_seconds is None:
                    await self._ack
                else:
                    await asyncio.wait_for(self._ack, self.options.ack_timeout_seconds)

            waiter = asyncio.create_task(wait_for_ack())
            cancelled = False
            try:
                while True:
                    try:
                        await asyncio.shield(waiter)
                        break
                    except asyncio.CancelledError:
                        if self.closed or waiter.cancelled():
                            waiter.cancel()
                            await asyncio.gather(waiter, return_exceptions=True)
                            raise
                        cancelled = True
            except TimeoutError as exc:
                self.failure = PersistenceDeliveryError("PersistenceTimeout", "Persistence acknowledgement timed out.")
                raise self.failure from exc
            self.acknowledged_sequence = batch.sequence
            self.acknowledged_revision = batch.revision
            self.last_acknowledged_batch = batch.batch_id
            # The publisher must commit its confirmed baseline before a
            # deferred cancellation can leave this publication boundary.
            return cancelled
        except BaseException:
            self.failed_batch_id = batch.batch_id
            raise
        finally:
            self.pending = None
            if self._ack is not None and not self._ack.done():
                self._ack.cancel()
            elif self._ack is not None and not self._ack.cancelled():
                self._ack.exception()
            self._ack = None

    def acknowledge(self, run_id: str, batch_id: str) -> bool:
        if self.closed or self.failure is not None or self.run_id != run_id:
            return False
        if batch_id == self.last_acknowledged_batch:
            return True
        if self.pending is None or self.pending.batch_id != batch_id or self._ack is None:
            return False
        if not self._ack.done():
            self._ack.set_result(None)
        return True

    def reject(self, run_id: str, batch_id: str, message: str) -> bool:
        if (self.closed or self.run_id != run_id or self.pending is None
                or self.pending.batch_id != batch_id or self._ack is None or self._ack.done()):
            return False
        self.failure = PersistenceDeliveryError("PersistenceRejected", message)
        self._ack.set_exception(self.failure)
        return True

    def finish(self):
        self.done = True
        self._ready.set()

    def close(self):
        self.closed = True
        self._space.set()
        self._presented.set()
        self._ready.set()
        if self._ack is not None and not self._ack.done():
            self._ack.cancel()
        self._queue.clear()
        self._overflow.clear()
