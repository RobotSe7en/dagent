"""Capability execution dispatcher for harness runtime."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dagent.capabilities.catalog import CapabilityCatalog
from dagent.capabilities.sandbox_context import current_run_execution
from dagent.capabilities.workspace import workspace_context
from dagent.schemas import ArtifactState, CapabilityInvocation, CapabilityResult, DAGNode


class CapabilityExecutionError(RuntimeError):
    """Raised when a capability cannot be executed."""


@dataclass(frozen=True)
class CapabilityExecutionContext:
    """Runtime context supplied to context-aware capability handlers."""

    task_id: str
    dag_id: str | None = None
    node: DAGNode | None = None
    spec_id: str | None = None
    workspace_path: str | Path | None = None
    input_artifacts: dict[str, list[str | Path]] = field(default_factory=dict)
    output_artifacts: dict[str, list[str | Path]] = field(default_factory=dict)
    artifact_states: dict[str, ArtifactState] = field(default_factory=dict)
    skills: tuple[str, ...] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    approved_boundary_invocation_id: str | None = None


@dataclass(frozen=True)
class CapabilityExecutionCallbacks:
    """Streaming callbacks supplied to context-aware capability handlers."""

    on_token: Callable[[str], None] | None = None
    on_event: Callable[[dict[str, Any]], None] | None = None


class CapabilityExecutor:
    """Executes invocations against a fixed capability catalog."""

    def __init__(self, catalog: CapabilityCatalog, workspace_root: str | Path | None = None) -> None:
        self.catalog = catalog
        self.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else catalog.workspace_root

    def workspace_context(self, workspace_root: str | Path | None):
        return workspace_context(workspace_root)

    async def execute(
        self,
        invocation: CapabilityInvocation,
        *,
        context: CapabilityExecutionContext | None = None,
        callbacks: CapabilityExecutionCallbacks | None = None,
    ) -> CapabilityResult:
        workspace_path = None if context is None else context.workspace_path
        with self.workspace_context(workspace_path):
            entry = self.catalog.get_entry(invocation.capability_id)
            if entry is None:
                raise CapabilityExecutionError(f"Capability '{invocation.capability_id}' is not registered.")
            definition = entry.definition
            if not definition.enabled:
                raise CapabilityExecutionError(f"Capability '{invocation.capability_id}' is disabled.")
            if definition.kind != invocation.kind:
                raise CapabilityExecutionError(
                    f"Capability '{invocation.capability_id}' has kind '{definition.kind}', "
                    f"not '{invocation.kind}'."
                )
            if current_run_execution() == "sandbox" and entry.sandbox_execution != "builtin_tool":
                # Fail closed: only ToolCapabilityProvider-owned tools execute
                # inside the sandbox. Other handlers, including public @tool
                # bindings, run on the host and must not be allowed to fall back.
                raise CapabilityExecutionError(
                    f"Capability '{invocation.capability_id}' (kind='{invocation.kind}') cannot "
                    "run under execution='sandbox', which currently supports only built-in tool "
                    "capabilities. Use execution='local'."
                )
            if entry.supports_context:
                args = (invocation,)
                kwargs = {
                    "context": context,
                    "callbacks": callbacks or CapabilityExecutionCallbacks(),
                }
            else:
                args = (invocation,)
                kwargs = {}
            if inspect.iscoroutinefunction(entry.handler):
                result = entry.handler(*args, **kwargs)
            else:
                result = await asyncio.to_thread(entry.handler, *args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
