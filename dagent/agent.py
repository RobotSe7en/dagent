"""Declarative public agent SDK configurations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dagent.capabilities.decorator import CapabilityBinding
from dagent.profiles import AgentProfile
from dagent.review import ReviewLevel


CapabilityRef = CapabilityBinding | str


@dataclass(frozen=True)
class ToolAgent:
    """Profile-backed tool-loop agent configuration."""

    profile: str | AgentProfile
    name: str | None = None
    max_steps: int = 8
    capabilities: Iterable[CapabilityRef] | None = None
    review: ReviewLevel = "fast"
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name or _default_profile_name(self.profile))
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1.")
        if self.capabilities is not None:
            object.__setattr__(self, "capabilities", tuple(self.capabilities))


@dataclass(frozen=True)
class DagAgent:
    """Dynamic DAG planner configuration."""

    planner_profile: str | AgentProfile = "dag_agent"
    name: str | None = None
    max_cycles: int = 6
    capabilities: Iterable[CapabilityRef] | None = None
    review: ReviewLevel = "fast"

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name or _default_profile_name(self.planner_profile))
        if self.max_cycles < 1:
            raise ValueError("max_cycles must be at least 1.")
        if self.capabilities is not None:
            object.__setattr__(self, "capabilities", tuple(self.capabilities))


def _default_profile_name(profile: str | AgentProfile) -> str:
    if isinstance(profile, AgentProfile):
        return profile.name
    return Path(profile).name
