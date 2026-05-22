"""Capability registration and providers."""

from dagent.capabilities.bootstrap import create_default_capability_catalog
from dagent.capabilities.catalog import CapabilityCatalog
from dagent.capabilities.decorator import CapabilityBinding, capability
from dagent.capabilities.mcp import MCPCapabilityProvider
from dagent.capabilities.providers import AgentCapabilityProvider, AgentNodeSessionStore
from dagent.capabilities.skills import SkillCapabilityProvider, SkillsCapabilityProvider
from dagent.capabilities.toolsets import CapabilityToolAdapter, CapabilityToolset

__all__ = [
    "AgentCapabilityProvider",
    "AgentNodeSessionStore",
    "CapabilityBinding",
    "CapabilityCatalog",
    "CapabilityToolAdapter",
    "CapabilityToolset",
    "MCPCapabilityProvider",
    "SkillCapabilityProvider",
    "SkillsCapabilityProvider",
    "capability",
    "create_default_capability_catalog",
]
