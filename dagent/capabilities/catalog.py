"""Catalog for executable capabilities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dagent.schemas import (
    CapabilityDefinition,
    CapabilityInvocation,
    CapabilityKind,
    CapabilityResult,
    validate_capability_id,
    validate_capability_name,
)
from dagent.config import DEFAULT_WORKSPACE


CapabilityHandlerResult = CapabilityResult | Awaitable[CapabilityResult]
CapabilityHandler = Callable[..., CapabilityHandlerResult]
ShutdownHook = Callable[[], None]
SandboxExecution = Literal["unsupported", "builtin_tool"]


@dataclass(frozen=True)
class CapabilityEntry:
    definition: CapabilityDefinition
    handler: CapabilityHandler
    supports_context: bool = False
    sandbox_execution: SandboxExecution = "unsupported"


class CapabilityCatalog:
    """Owns capability definitions and their executable handlers."""

    def __init__(self, *, workspace_root: str | Path = DEFAULT_WORKSPACE) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self._entries: dict[str, CapabilityEntry] = {}
        self._ids_by_name: dict[str, str] = {}
        self._shutdown_hooks: list[ShutdownHook] = []
        self._shutdown_complete = False

    def register(
        self,
        definition: CapabilityDefinition,
        handler: CapabilityHandler,
        *,
        supports_context: bool = False,
        sandbox_execution: SandboxExecution = "unsupported",
    ) -> None:
        self.validate_registerable(definition)
        stored = definition.model_copy(deep=True)
        self._entries[stored.id] = CapabilityEntry(
            definition=stored,
            handler=handler,
            supports_context=supports_context,
            sandbox_execution=sandbox_execution,
        )
        self._ids_by_name[stored.name] = stored.id

    def replace(
        self,
        definition: CapabilityDefinition,
        handler: CapabilityHandler,
        *,
        supports_context: bool = False,
        sandbox_execution: SandboxExecution = "unsupported",
    ) -> None:
        current = self._entries.get(definition.id)
        if current is None:
            raise KeyError(f"Capability '{definition.id}' is not registered.")
        self.validate_registerable(definition, ignore_ids=(definition.id,))
        stored = definition.model_copy(deep=True)
        if current.definition.name != stored.name:
            self._ids_by_name.pop(current.definition.name, None)
        self._entries[stored.id] = CapabilityEntry(
            definition=stored,
            handler=handler,
            supports_context=supports_context,
            sandbox_execution=sandbox_execution,
        )
        self._ids_by_name[stored.name] = stored.id

    def set_enabled(self, capability_id: str, enabled: bool) -> CapabilityDefinition:
        entry = self._entries.get(capability_id)
        if entry is None:
            raise KeyError(f"Capability '{capability_id}' is not registered.")
        updated = entry.definition.model_copy(update={"enabled": enabled}, deep=True)
        self._entries[capability_id] = CapabilityEntry(
            definition=updated,
            handler=entry.handler,
            supports_context=entry.supports_context,
            sandbox_execution=entry.sandbox_execution,
        )
        return updated

    def delete(self, capability_id: str) -> None:
        entry = self._entries.pop(capability_id, None)
        if entry is not None:
            self._ids_by_name.pop(entry.definition.name, None)

    def validate_registerable(
        self,
        definition: CapabilityDefinition,
        *,
        ignore_ids: Iterable[str] = (),
    ) -> None:
        ignored = set(ignore_ids)
        validate_capability_id(definition.id, kind=definition.kind)
        validate_capability_name(definition.name)
        if definition.id in self._entries and definition.id not in ignored:
            raise ValueError(f"Capability '{definition.id}' is already registered.")
        existing_id = self._ids_by_name.get(definition.name)
        if existing_id is not None and existing_id not in ignored:
            raise ValueError(
                f"Capability name '{definition.name}' is already registered by '{existing_id}'."
            )

    def validate_registerable_batch(
        self,
        definitions: Iterable[CapabilityDefinition],
        *,
        ignore_ids: Iterable[str] = (),
    ) -> None:
        ignored = set(ignore_ids)
        seen_ids: set[str] = set()
        seen_names: dict[str, str] = {}
        for definition in definitions:
            if definition.id in seen_ids:
                raise ValueError(f"Capability '{definition.id}' is already registered.")
            existing_seen_id = seen_names.get(definition.name)
            if existing_seen_id is not None:
                raise ValueError(
                    f"Capability name '{definition.name}' is already registered by '{existing_seen_id}'."
                )
            self.validate_registerable(definition, ignore_ids=ignored)
            seen_ids.add(definition.id)
            seen_names[definition.name] = definition.id

    def restore_entries(self, entries: Mapping[str, CapabilityEntry | None]) -> list[str]:
        restored_ids: list[str] = []
        try:
            for capability_id, entry in entries.items():
                if entry is None:
                    continue
                if capability_id != entry.definition.id:
                    raise ValueError(
                        f"Cannot restore capability entry '{capability_id}' with definition '{entry.definition.id}'."
                    )
                self.validate_registerable(entry.definition)
                self._entries[capability_id] = entry
                self._ids_by_name[entry.definition.name] = capability_id
                restored_ids.append(capability_id)
        except Exception:
            for capability_id in restored_ids:
                self.delete(capability_id)
            raise
        return restored_ids

    def get(self, capability_id: str) -> CapabilityDefinition | None:
        entry = self._entries.get(capability_id)
        return entry.definition.model_copy(deep=True) if entry is not None else None

    def get_entry(self, capability_id: str) -> CapabilityEntry | None:
        return self._entries.get(capability_id)

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

    def ids(self) -> set[str]:
        return set(self._entries)

    def add_shutdown_hook(self, hook: ShutdownHook) -> None:
        if hook not in self._shutdown_hooks:
            self._shutdown_hooks.append(hook)

    def remove_shutdown_hook(self, hook: ShutdownHook) -> None:
        try:
            self._shutdown_hooks.remove(hook)
        except ValueError:
            pass

    def shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        hooks = list(self._shutdown_hooks)
        self._shutdown_hooks.clear()
        for hook in hooks:
            hook()
