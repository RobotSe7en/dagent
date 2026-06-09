"""Per-request capability visibility for runtime loops."""

from __future__ import annotations

from dataclasses import dataclass

from dagent.schemas import RunCapabilityScope


@dataclass(frozen=True)
class CapabilityScope:
    """Restricts the capabilities and skills visible to one run."""

    capability_ids: tuple[str, ...] | None = None
    skills: tuple[str, ...] | None = None


DEFAULT_CAPABILITY_SCOPE = CapabilityScope()


def capability_scope_to_state(scope: CapabilityScope) -> RunCapabilityScope:
    return RunCapabilityScope(
        capability_ids=scope.capability_ids,
        skills=scope.skills,
    )


def capability_scope_from_state(scope: RunCapabilityScope) -> CapabilityScope:
    return CapabilityScope(
        capability_ids=scope.capability_ids,
        skills=scope.skills,
    )
