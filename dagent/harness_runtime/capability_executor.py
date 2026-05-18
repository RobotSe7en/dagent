"""Capability execution dispatcher for harness runtime."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dagent.capabilities.catalog import CapabilityCatalog
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
    metadata: dict[str, Any] = field(default_factory=dict)


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
        if entry.supports_context:
            result = entry.handler(
                invocation,
                context=context,
                callbacks=callbacks or CapabilityExecutionCallbacks(),
            )
        else:
            result = entry.handler(invocation)
        if inspect.isawaitable(result):
            return await result
        return result
