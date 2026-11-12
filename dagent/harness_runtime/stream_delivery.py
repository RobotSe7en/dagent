"""Unbounded ordered event delivery with one outstanding durable acknowledgement."""

from __future__ import annotations

import asyncio
from dataclasses import replace
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


class StreamDelivery:
    def __init__(self, options: StreamPersistenceOptions) -> None:
        self.options = options
        self.delivery_id = f"delivery_{uuid4().hex}"
        self.run_id: str | None = None
        self.producer_task: asyncio.Task[None] | None = None
        self.sequence = 0
        self.acknowledged_sequence = 0
        self.acknowledged_revision: int | None = None
        self.last_acknowledged_batch: str | None = None
        self.pending: ConversationChangeBatch | None = None
        self.failure: PersistenceDeliveryError | None = None
        self.failed_batch_id: str | None = None
        self.terminal_started = False
        # Set once the host confirms the terminal batch; only synchronous
        # producer cleanup remains after that.
        self.terminal_acknowledged = False
        self.closed = False
        self.done = False
        self._queue: asyncio.Queue[RunStreamEvent | None] = asyncio.Queue()
        self._ack: asyncio.Future[None] | None = None
        self._presented = asyncio.Event()

    def check(self) -> None:
        if self.failure is not None:
            raise self.failure
        if self.closed:
            raise PersistenceDeliveryError("DeliveryClosed", "Persistent stream is closed.")

    def emit(self, event: RunStreamEvent) -> None:
        """Accept a synchronous producer event without blocking or raising.

        A failed or closed delivery drops the event; failures surface at durable
        batch publication instead of inside a model call.
        """
        if self.closed or self.failure is not None or event.type in _DIAGNOSTIC_EVENTS:
            return
        if event.type == "model.call.observed":
            event = replace(event, data=replace(event.data, model_call=None))
        self._queue.put_nowait(event)

    async def receive(self) -> RunStreamEvent | None:
        if self.closed or (self.done and self._queue.empty()):
            return None
        event = await self._queue.get()
        if event is None:
            return None
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
            self._queue.put_nowait(RunStreamEvent(type="conversation.changed", data=batch.model_copy(deep=True), run_id=batch.run_id))

            async def wait_for_ack() -> None:
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
        if self._ack.cancelled():
            return False
        if self._ack.done():
            return self._ack.exception() is None
        self._ack.set_result(None)
        if self.pending.status is not None:
            self.terminal_acknowledged = True
        return True

    def reject(self, run_id: str, batch_id: str, message: str) -> bool:
        if (self.closed or self.run_id != run_id or self.pending is None
                or self.pending.batch_id != batch_id or self._ack is None or self._ack.done()):
            return False
        self.failure = PersistenceDeliveryError("PersistenceRejected", message)
        self._ack.set_exception(self.failure)
        return True

    def finish(self) -> None:
        self.done = True
        self._queue.put_nowait(None)

    def close(self) -> None:
        self.closed = True
        self._presented.set()
        if self._ack is not None and not self._ack.done():
            self._ack.cancel()
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)
