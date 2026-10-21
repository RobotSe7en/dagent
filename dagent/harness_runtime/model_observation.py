"""Request-scoped observation and budget-owned provider re-preparation."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from time import monotonic
from typing import Any
from uuid import uuid4

from dagent.harness_runtime.execution_usage import record_model_turn
from dagent.providers.model_io import (
    ModelRequest, ModelStreamEvent, complete_model, stream_model,
)
from dagent.providers.openai_compatible import ProviderReprepareRequired, ProviderResponseError


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
    seen = {prepared.fingerprint} if prepared is not None else set()
    changes = 0
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
            if emitted or prepared is None or reprepare is None or changes >= 4:
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
        replacement = await reprepare()
        if replacement.fingerprint in seen:
            raise retry
        seen.add(replacement.fingerprint)
        prepared = replacement
        changes += 1
