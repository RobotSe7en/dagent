"""Request-scoped observation and budget-owned provider re-preparation."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from time import monotonic
from typing import Any
from uuid import uuid4

from dagent.harness_runtime.execution_usage import record_model_turn
from dagent.providers.model_io import (
    ModelRequest, ModelStreamEvent, ModelTokenCount, PreparedModelCall,
    complete_model, stream_model,
)
from dagent.providers.openai_compatible import ProviderReprepareRequired, ProviderResponseError


class _ReprepareGuard:
    """Require progress and at most four replacements in a preparation chain."""

    def __init__(self, prepared: Any) -> None:
        self.seen = {prepared.fingerprint} if prepared is not None else set()
        self.changes = 0

    async def replace(
        self, reprepare: Callable[[], Awaitable[Any]], error: ProviderReprepareRequired,
    ) -> Any:
        if self.changes >= 4:
            raise error
        replacement = await reprepare()
        if replacement.fingerprint in self.seen:
            raise error
        self.seen.add(replacement.fingerprint)
        self.changes += 1
        return replacement


async def count_prepared_call(
    prepared: PreparedModelCall, *,
    counter: Callable[[PreparedModelCall], Awaitable[ModelTokenCount | None]],
    reprepare: Callable[[], Awaitable[PreparedModelCall]],
) -> tuple[PreparedModelCall, ModelTokenCount | None]:
    """Count replacements on explicit signals; the caller owns the budget gate."""
    guard = None
    while True:
        try:
            return prepared, await counter(prepared)
        except ProviderReprepareRequired as exc:
            if guard is None:
                guard = _ReprepareGuard(prepared)
            prepared = await guard.replace(reprepare, exc)


class ContextObserver:
    def __init__(self, enabled: bool, emit: Callable | None, run_id: str | None) -> None:
        self.enabled = enabled and emit is not None
        self.emit = emit
        self.run_id = run_id
        self.compaction_id = f"compaction_{uuid4().hex}"
        self.started = False
        self.last_progress = 0.0
        self.characters = 0

    def event(self, kind: str, **data: Any) -> None:
        if self.enabled:
            self.emit({"type": kind, "run_id": self.run_id, **data})

    def start(self, item_count: int) -> None:
        if not self.started:
            self.started = True
            self.event("context_compaction_started", item_count=item_count,
                       compaction_id=self.compaction_id)

    def progress(self, content: str = "", *, completed: bool = False) -> None:
        self.characters += len(content)
        now = monotonic()
        if (content or completed) and (completed or now - self.last_progress >= 1):
            self.last_progress = now
            self.event("context_compaction_progress", compaction_id=self.compaction_id,
                       received_characters=self.characters, request_completed=completed)

    def end(self, outcome: str) -> None:
        if self.started:
            self.event("context_compaction_ended", compaction_id=self.compaction_id, outcome=outcome)
            self.started = False
            self.compaction_id = f"compaction_{uuid4().hex}"
            self.characters = 0
            self.last_progress = 0.0


async def provider_events(
    provider: Any, request: ModelRequest, *, stream: bool, prepared: Any = None,
    reprepare: Callable[[], Awaitable[Any]] | None = None,
    observer: ContextObserver | None = None, before_attempt: Callable[[], None] | None = None,
) -> AsyncIterator[ModelStreamEvent]:
    """Send counted snapshots; only the budget owner may prepare replacements."""
    guard = _ReprepareGuard(prepared)
    while True:
        if before_attempt is not None:
            before_attempt()
        record_model_turn()
        started = monotonic()
        call_id = f"call_{uuid4().hex}"
        response = None
        error = None
        emitted = False
        retry = None
        try:
            if stream:
                events = (provider.stream_prepared(prepared) if prepared is not None
                          else stream_model(provider, request))
                try:
                    async for event in events:
                        if event.response is not None:
                            response = event.response
                        if observer is not None and request.purpose == "compaction":
                            observer.progress(event.content)
                        emitted = True
                        yield event
                finally:
                    await events.aclose()
            else:
                response = (await provider.complete_prepared(prepared) if prepared is not None
                            else await complete_model(provider, request))
                yield ModelStreamEvent(type="done", response=response)
            return
        except ProviderReprepareRequired as exc:
            error = exc
            if emitted or prepared is None or reprepare is None:
                raise
            retry = exc
        except BaseException as exc:
            error = exc
            raise
        finally:
            if observer is not None:
                if error is None and response is None:
                    error = ProviderResponseError("incomplete", reason="missing_terminal_response")
                snapshot = response if response is not None else getattr(error, "response", None)
                metadata = (snapshot.metadata if snapshot is not None and snapshot.metadata is not None
                            else getattr(error, "metadata", None))
                observer.event(
                    "model_call_observed", call_id=call_id, purpose=request.purpose,
                    usage=snapshot.usage if snapshot is not None else None,
                    model_call=metadata, elapsed_seconds=monotonic() - started,
                    status="failed" if error is not None else "completed",
                    error_type=type(error).__name__ if error is not None else None,
                )
                if request.purpose == "compaction" and error is None:
                    observer.progress(completed=True)
        prepared = await guard.replace(reprepare, retry)
