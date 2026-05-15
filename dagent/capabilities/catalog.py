"""Catalog for executable capabilities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dagent.schemas import (
    CapabilityDefinition,
    CapabilityInvocation,
    CapabilityKind,
    CapabilityResult,
)


CapabilityHandler = Callable[[CapabilityInvocation], CapabilityResult]


@dataclass(frozen=True)
class CapabilityEntry:
    definition: CapabilityDefinition
    handler: CapabilityHandler


class CapabilityCatalog:
    """Owns capability definitions and their executable handlers."""

    def __init__(self, *, workspace_root: str | Path = ".") -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self._entries: dict[str, CapabilityEntry] = {}

    def register(self, definition: CapabilityDefinition, handler: CapabilityHandler) -> None:
        if definition.id in self._entries:
            raise ValueError(f"Capability '{definition.id}' is already registered.")
        self._entries[definition.id] = CapabilityEntry(
            definition=definition.model_copy(deep=True),
            handler=handler,
        )

    def replace(self, definition: CapabilityDefinition, handler: CapabilityHandler) -> None:
        if definition.id not in self._entries:
            raise KeyError(f"Capability '{definition.id}' is not registered.")
        self._entries[definition.id] = CapabilityEntry(
            definition=definition.model_copy(deep=True),
            handler=handler,
        )

    def set_enabled(self, capability_id: str, enabled: bool) -> CapabilityDefinition:
        entry = self._entries.get(capability_id)
        if entry is None:
            raise KeyError(f"Capability '{capability_id}' is not registered.")
        updated = entry.definition.model_copy(update={"enabled": enabled}, deep=True)
        self._entries[capability_id] = CapabilityEntry(definition=updated, handler=entry.handler)
        return updated

    def delete(self, capability_id: str) -> None:
        self._entries.pop(capability_id, None)

    def get(self, capability_id: str) -> CapabilityDefinition | None:
        entry = self._entries.get(capability_id)
        return entry.definition.model_copy(deep=True) if entry is not None else None

    def get_entry(self, capability_id: str) -> CapabilityEntry | None:
        return self._entries.get(capability_id)

    def handler_for(self, capability_id: str) -> CapabilityHandler | None:
        entry = self._entries.get(capability_id)
        return entry.handler if entry is not None else None

    def get_by_name(self, name: str, *, kind: CapabilityKind | None = None) -> CapabilityDefinition | None:
        for entry in self._entries.values():
            definition = entry.definition
            if definition.name == name and (kind is None or definition.kind == kind):
                return definition.model_copy(deep=True)
        return None

    def list(self, *, kind: CapabilityKind | None = None, enabled_only: bool = False) -> list[CapabilityDefinition]:
        definitions = [entry.definition for entry in self._entries.values()]
        if kind is not None:
            definitions = [definition for definition in definitions if definition.kind == kind]
        if enabled_only:
            definitions = [definition for definition in definitions if definition.enabled]
        return sorted(
            [definition.model_copy(deep=True) for definition in definitions],
            key=lambda definition: definition.id,
        )

    def names(self, *, kind: CapabilityKind | None = None) -> set[str]:
        return {definition.name for definition in self.list(kind=kind)}

    def ids(self) -> set[str]:
        return set(self._entries)
