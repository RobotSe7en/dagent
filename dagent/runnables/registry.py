"""Registry for runnable capabilities."""

from __future__ import annotations

from dagent.runnables.schemas import RunnableDefinition, RunnableKind


class RunnableRegistry:
    """Stores enabled runnable definitions by stable id."""

    def __init__(self) -> None:
        self._definitions: dict[str, RunnableDefinition] = {}

    def register(self, definition: RunnableDefinition) -> None:
        if definition.id in self._definitions:
            raise ValueError(f"Runnable '{definition.id}' is already registered.")
        self._definitions[definition.id] = definition

    def update(self, definition: RunnableDefinition) -> None:
        self._definitions[definition.id] = definition

    def delete(self, runnable_id: str) -> None:
        self._definitions.pop(runnable_id, None)

    def get(self, runnable_id: str) -> RunnableDefinition | None:
        return self._definitions.get(runnable_id)

    def list(self, *, kind: RunnableKind | None = None, enabled_only: bool = False) -> list[RunnableDefinition]:
        definitions = list(self._definitions.values())
        if kind is not None:
            definitions = [definition for definition in definitions if definition.kind == kind]
        if enabled_only:
            definitions = [definition for definition in definitions if definition.enabled]
        return sorted(definitions, key=lambda definition: definition.id)

    def names(self) -> set[str]:
        return {definition.name for definition in self._definitions.values()}

    def ids(self) -> set[str]:
        return set(self._definitions)
