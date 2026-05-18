"""Context-local capability workspace root."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from pathlib import Path


_WORKSPACE_ROOT: ContextVar[Path | None] = ContextVar("dagent_workspace_root", default=None)


def current_workspace_root(default: str | Path) -> Path:
    return (_WORKSPACE_ROOT.get() or Path(default)).resolve()


@contextmanager
def workspace_context(workspace_root: str | Path | None) -> Iterator[None]:
    if workspace_root is None:
        yield
        return
    token = _WORKSPACE_ROOT.set(Path(workspace_root).resolve())
    try:
        yield
    finally:
        _WORKSPACE_ROOT.reset(token)
