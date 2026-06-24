"""LLM-visible capability toolsets."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Sequence

from dagent.capabilities.catalog import CapabilityCatalog
from dagent.providers import ToolCall
from dagent.schemas import Boundary, CapabilityDefinition, CapabilityInvocation


BUILTIN_CAPABILITY_IDS = (
    "tool.read_file",
    "tool.write_file",
    "tool.edit_file",
    "tool.list_files",
    "tool.grep",
    "tool.shell",
)
_FUNCTION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class CapabilityToolset:
    name: str
    capability_ids: tuple[str, ...]


class CapabilityToolAdapter:
    """Adapts enabled capabilities into LLM tool schemas and invocations."""

    def __init__(
        self,
        catalog: CapabilityCatalog,
        *,
        toolsets: Sequence[CapabilityToolset] | None = None,
    ) -> None:
        self.catalog = catalog
        resolved_toolsets = (
            [CapabilityToolset("builtin", BUILTIN_CAPABILITY_IDS)]
            if toolsets is None
            else toolsets
        )
        self._toolsets = {toolset.name: toolset for toolset in resolved_toolsets}

    def capabilities(
        self,
        enabled_toolsets: Sequence[str],
        *,
        capability_ids: Sequence[str] | None = None,
    ) -> list[CapabilityDefinition]:
        return [
            definition.model_copy(update={"name": self.function_name(definition)}, deep=True)
            for definition in self._definitions(enabled_toolsets, capability_ids=capability_ids)
        ]

    def definitions(
        self,
        enabled_toolsets: Sequence[str],
        *,
        capability_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.parameters or {"type": "object"},
                },
            }
            for definition in self.capabilities(enabled_toolsets, capability_ids=capability_ids)
        ]

    def invocation_from_tool_call(
        self,
        tool_call: ToolCall,
        boundary: Boundary,
        *,
        enabled_toolsets: Sequence[str],
        capability_ids: Sequence[str] | None = None,
    ) -> CapabilityInvocation:
        definition = self.definition_from_tool_call(
            tool_call,
            enabled_toolsets=enabled_toolsets,
            capability_ids=capability_ids,
        )
        return CapabilityInvocation(
            invocation_id=tool_call.id,
            capability_id=definition.id,
            kind=definition.kind,
            arguments=tool_call.arguments,
            boundary=boundary,
            risk=definition.policy.risk,
        )

    def definition_from_tool_call(
        self,
        tool_call: ToolCall,
        *,
        enabled_toolsets: Sequence[str],
        capability_ids: Sequence[str] | None = None,
    ) -> CapabilityDefinition:
        definitions_by_name = self._definitions_by_function_name(
            enabled_toolsets,
            capability_ids=capability_ids,
        )
        definition = definitions_by_name.get(tool_call.name)
        if definition is None:
            available = ", ".join(sorted(definitions_by_name))
            raise KeyError(
                f"Capability tool '{tool_call.name}' is not enabled. "
                f"Available tools: {available}."
            )
        return definition

    def ensure_allowed(
        self,
        capability_id: str,
        *,
        enabled_toolsets: Sequence[str],
        capability_ids: Sequence[str] | None = None,
    ) -> CapabilityDefinition:
        definitions_by_id = {
            definition.id: definition
            for definition in self._definitions(enabled_toolsets, capability_ids=capability_ids)
        }
        definition = definitions_by_id.get(capability_id)
        if definition is None:
            available = ", ".join(sorted(definitions_by_id))
            raise KeyError(
                f"Capability '{capability_id}' is not enabled. "
                f"Available capabilities: {available}."
            )
        return definition

    def reviewable_names(
        self,
        enabled_toolsets: Sequence[str],
        *,
        capability_ids: Sequence[str] | None = None,
    ) -> set[str]:
        return {
            self.function_name(definition)
            for definition in self._definitions(enabled_toolsets, capability_ids=capability_ids)
            if definition.policy.risk in {"medium", "high"} or definition.policy.requires_review
        }

    def function_name_for_capability(
        self,
        capability_id: str,
        *,
        enabled_toolsets: Sequence[str],
        capability_ids: Sequence[str] | None = None,
    ) -> str:
        definition = self.ensure_allowed(
            capability_id,
            enabled_toolsets=enabled_toolsets,
            capability_ids=capability_ids,
        )
        return self.function_name(definition)

    def function_name(self, definition: CapabilityDefinition) -> str:
        return capability_function_name(definition)

    def _definitions(
        self,
        enabled_toolsets: Sequence[str],
        *,
        capability_ids: Sequence[str] | None = None,
    ) -> list[CapabilityDefinition]:
        resolved_ids = self._capability_ids(enabled_toolsets)
        if capability_ids is not None:
            available = set(resolved_ids)
            unknown = [capability_id for capability_id in capability_ids if capability_id not in available]
            if unknown:
                raise KeyError(f"Capability '{unknown[0]}' is not registered in enabled toolsets.")
            seen: set[str] = set()
            resolved_ids = []
            for capability_id in capability_ids:
                if capability_id not in seen:
                    seen.add(capability_id)
                    resolved_ids.append(capability_id)
        definitions: list[CapabilityDefinition] = []
        for capability_id in resolved_ids:
            definition = self.catalog.get(capability_id)
            if definition is None:
                raise KeyError(f"Capability '{capability_id}' is not registered.")
            if definition.enabled:
                definitions.append(definition)
        self._check_name_collisions(definitions)
        return definitions

    def _capability_ids(self, enabled_toolsets: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        capability_ids: list[str] = []
        for toolset_name in enabled_toolsets:
            toolset = self._toolsets.get(toolset_name)
            if toolset is None:
                raise KeyError(f"Capability toolset '{toolset_name}' is not registered.")
            for capability_id in toolset.capability_ids:
                if capability_id not in seen:
                    seen.add(capability_id)
                    capability_ids.append(capability_id)
        return capability_ids

    def _definitions_by_function_name(
        self,
        enabled_toolsets: Sequence[str],
        *,
        capability_ids: Sequence[str] | None = None,
    ) -> dict[str, CapabilityDefinition]:
        return {
            self.function_name(definition): definition
            for definition in self._definitions(enabled_toolsets, capability_ids=capability_ids)
        }

    def _check_name_collisions(self, definitions: Sequence[CapabilityDefinition]) -> None:
        ensure_unique_capability_function_names(definitions)


def capability_function_name(definition: CapabilityDefinition) -> str:
    if _FUNCTION_NAME_RE.match(definition.name):
        return definition.name
    name = re.sub(r"[^A-Za-z0-9_]+", "_", definition.id).strip("_")
    if not name or not re.match(r"^[A-Za-z_]", name):
        name = f"capability_{name}"
    return name


def ensure_unique_capability_function_names(definitions: Sequence[CapabilityDefinition]) -> None:
    seen: dict[str, str] = {}
    for definition in definitions:
        name = capability_function_name(definition)
        previous = seen.get(name)
        if previous is not None:
            raise ValueError(
                "LLM tool name collision: "
                f"'{previous}' and '{definition.id}' both map to '{name}'."
            )
        seen[name] = definition.id
