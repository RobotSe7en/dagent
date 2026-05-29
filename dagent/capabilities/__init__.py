"""Capability registration and providers."""

from dagent.capabilities.bootstrap import create_default_capability_catalog
from dagent.capabilities.catalog import CapabilityCatalog
from dagent.capabilities.decorator import CapabilityBinding, capability, tool
from dagent.capabilities.mcp import MCPCapabilityProvider
from dagent.capabilities.providers import AgentCapabilityProvider, AgentNodeSessionStore
from dagent.capabilities.skills import (
    SkillAmbiguousError,
    SkillEntry,
    SkillNotFoundError,
    SkillPermissionError,
    SkillStore,
    SkillStoreError,
    SkillView,
    SkillsCapabilityProvider,
    default_managed_skill_root,
    default_skill_roots,
)
from dagent.capabilities.toolsets import CapabilityToolAdapter, CapabilityToolset

__all__ = [
    "AgentCapabilityProvider",
    "AgentNodeSessionStore",
    "CapabilityBinding",
    "CapabilityCatalog",
    "CapabilityToolAdapter",
    "CapabilityToolset",
    "MCPCapabilityProvider",
    "SkillAmbiguousError",
    "SkillEntry",
    "SkillNotFoundError",
    "SkillPermissionError",
    "SkillStore",
    "SkillStoreError",
    "SkillView",
    "SkillsCapabilityProvider",
    "capability",
    "create_default_capability_catalog",
    "default_managed_skill_root",
    "default_skill_roots",
    "tool",
]
