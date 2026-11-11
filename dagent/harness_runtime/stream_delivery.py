"""Bounded event delivery and one outstanding durable acknowledgement."""

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
        self._queue: deque[tuple[RunStreamEvent, int]] = deque()
        self._staged: deque[RunStreamEvent] = deque()
        self._bytes = 0
        self.peak_ready_events = 0
        self.peak_ready_bytes = 0
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
        self.check()
        if event.type in {"trace.updated", "dag.updated", "capability.call.started",
                          "capability.call.completed", "capability.call.failed", "review.required"}:
            return
        if event.type == "model.call.observed":
            event = replace(event, data=replace(event.data, model_call=None))
        if len(self._staged) >= max(32, self.options.max_buffer_events):
            self.failure = PersistenceDeliveryError("StreamBufferOverflow", "Synchronous event producer exceeded its bounded emission group.")
            raise self.failure
        self._staged.append(event)

    async def checkpoint(self):
        self.check()
        while self._staged:
            event = self._staged[0]
            size = _text_bytes(event.data)
            while self._queue and (len(self._queue) >= self.options.max_buffer_events
                                  or self._bytes + size > self.options.max_buffer_bytes):
                self._space.clear()
                await self._space.wait()
                self.check()
            self._staged.popleft()
            self._queue.append((event, size))
            self._bytes += size
            self.peak_ready_events = max(self.peak_ready_events, len(self._queue))
            self.peak_ready_bytes = max(self.peak_ready_bytes, self._bytes)
            self._ready.set()

    async def receive(self) -> RunStreamEvent | None:
        while not self._queue:
            if self.done:
                return None
            self._ready.clear()
            await self._ready.wait()
        event, size = self._queue.popleft()
        self._bytes -= size
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
            self.emit(RunStreamEvent(type="conversation.changed", data=batch.model_copy(deep=True), run_id=batch.run_id))
            async def wait_for_ack():
                await self.checkpoint()
                await self._presented.wait()
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
        self._staged.clear()
