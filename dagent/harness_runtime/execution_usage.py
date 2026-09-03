"""Run-scoped, concurrency-safe execution usage accounting."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from typing import Iterator

from dagent.schemas import ExecutionUsage


class ExecutionUsageTracker:
    """Mutable operation counters shared by every nested execution path."""

    def __init__(self, usage: ExecutionUsage | None = None) -> None:
        initial = usage or ExecutionUsage()
        self._model_turns = initial.model_turns
        self._capability_calls = initial.capability_calls
        self._lock = Lock()

    def record_model_turn(self) -> None:
        with self._lock:
            self._model_turns += 1

    def record_capability_call(self) -> None:
        with self._lock:
            self._capability_calls += 1

    def snapshot(self) -> ExecutionUsage:
        with self._lock:
            return ExecutionUsage(
                total_operations=self._model_turns + self._capability_calls,
                model_turns=self._model_turns,
                capability_calls=self._capability_calls,
            )


_CURRENT_EXECUTION_USAGE: ContextVar[ExecutionUsageTracker | None] = ContextVar(
    "dagent_execution_usage",
    default=None,
)


@contextmanager
def execution_usage_scope(tracker: ExecutionUsageTracker) -> Iterator[None]:
    token = _CURRENT_EXECUTION_USAGE.set(tracker)
    try:
        yield
    finally:
        _CURRENT_EXECUTION_USAGE.reset(token)


def record_model_turn() -> None:
    tracker = _CURRENT_EXECUTION_USAGE.get()
    if tracker is not None:
        tracker.record_model_turn()


def record_capability_call() -> None:
    tracker = _CURRENT_EXECUTION_USAGE.get()
    if tracker is not None:
        tracker.record_capability_call()
