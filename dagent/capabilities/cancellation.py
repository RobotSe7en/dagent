"""Run-scoped cooperative cancellation for blocking capability handlers."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event
from typing import Iterator


_RUN_CANCELLATION_EVENT: ContextVar[Event | None] = ContextVar(
    "dagent_run_cancellation_event",
    default=None,
)


@contextmanager
def run_cancellation_context(event: Event) -> Iterator[None]:
    token = _RUN_CANCELLATION_EVENT.set(event)
    try:
        yield
    finally:
        _RUN_CANCELLATION_EVENT.reset(token)


def current_run_cancellation_event() -> Event | None:
    return _RUN_CANCELLATION_EVENT.get()
