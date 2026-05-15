"""Runnable execution dispatcher."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from dagent.runnables.registry import RunnableRegistry
from dagent.runnables.schemas import RunnableInvocation, RunnableResult


RunnableHandler = Callable[[RunnableInvocation], RunnableResult]


class RunnableExecutionError(RuntimeError):
    """Raised when a runnable cannot be executed."""


class RunnableExecutor:
    """Executes runnable invocations through registered backend handlers."""

    def __init__(self, registry: RunnableRegistry, workspace_root: str | Path = ".") -> None:
        self.registry = registry
        self.workspace_root = Path(workspace_root).resolve()
        self._handlers: dict[str, RunnableHandler] = {}

    def register_handler(self, runnable_id: str, handler: RunnableHandler) -> None:
        if runnable_id in self._handlers:
            raise ValueError(f"Runnable handler '{runnable_id}' is already registered.")
        self._handlers[runnable_id] = handler

    def execute(self, invocation: RunnableInvocation) -> RunnableResult:
        definition = self.registry.get(invocation.runnable_id)
        if definition is None:
            raise RunnableExecutionError(f"Runnable '{invocation.runnable_id}' is not registered.")
        if not definition.enabled:
            raise RunnableExecutionError(f"Runnable '{invocation.runnable_id}' is disabled.")
        if definition.kind != invocation.kind:
            raise RunnableExecutionError(
                f"Runnable '{invocation.runnable_id}' has kind '{definition.kind}', "
                f"not '{invocation.kind}'."
            )
        handler = self._handlers.get(invocation.runnable_id)
        if handler is None:
            raise RunnableExecutionError(f"Runnable '{invocation.runnable_id}' has no handler.")
        return handler(invocation)
