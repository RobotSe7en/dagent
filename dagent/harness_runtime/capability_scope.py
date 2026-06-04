"""Per-request capability visibility for runtime loops."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityScope:
    """Restricts the capabilities and skills visible to one run."""

    capability_ids: tuple[str, ...] | None = None
    skills: tuple[str, ...] | None = None


DEFAULT_CAPABILITY_SCOPE = CapabilityScope()
