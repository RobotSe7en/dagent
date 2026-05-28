"""Per-request capability visibility for runtime loops."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CapabilityScope:
    """Restricts the capabilities and skill instructions visible to one run."""

    capability_ids: tuple[str, ...] | None = None
    skill_instructions: tuple[str, ...] = field(default_factory=tuple)


DEFAULT_CAPABILITY_SCOPE = CapabilityScope()
