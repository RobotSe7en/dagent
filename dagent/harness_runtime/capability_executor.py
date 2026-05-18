"""Capability execution dispatcher for harness runtime."""

from __future__ import annotations

from pathlib import Path

from dagent.capabilities.catalog import CapabilityCatalog
from dagent.capabilities.workspace import workspace_context
from dagent.schemas import CapabilityInvocation, CapabilityResult


class CapabilityExecutionError(RuntimeError):
    """Raised when a capability cannot be executed."""


class CapabilityExecutor:
    """Executes invocations against a fixed capability catalog."""

    def __init__(self, catalog: CapabilityCatalog, workspace_root: str | Path | None = None) -> None:
        self.catalog = catalog
        self.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else catalog.workspace_root

    def workspace_context(self, workspace_root: str | Path | None):
        return workspace_context(workspace_root)

    def execute(self, invocation: CapabilityInvocation) -> CapabilityResult:
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
        return entry.handler(invocation)
