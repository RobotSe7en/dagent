"""Context-local sandbox execution state.

Kept dependency-light (no docker import) so it can be imported anywhere. The
active :class:`SandboxSession` is stored as ``Any`` to avoid importing the
docker-backed engine here.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from typing import Any

from dagent.schemas import RunExecution


_RUN_EXECUTION: ContextVar[RunExecution] = ContextVar("dagent_run_execution", default="local")
_SANDBOX_SESSION: ContextVar[Any | None] = ContextVar("dagent_sandbox_session", default=None)


def current_run_execution() -> RunExecution:
    return _RUN_EXECUTION.get()


def current_sandbox_session() -> Any | None:
    return _SANDBOX_SESSION.get()


@contextmanager
def run_execution_context(execution: RunExecution) -> Iterator[None]:
    token = _RUN_EXECUTION.set(execution)
    try:
        yield
    finally:
        _RUN_EXECUTION.reset(token)


@contextmanager
def sandbox_session_context(session: Any | None) -> Iterator[None]:
    token = _SANDBOX_SESSION.set(session)
    try:
        yield
    finally:
        _SANDBOX_SESSION.reset(token)
