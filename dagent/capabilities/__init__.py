"""Capability registration and providers."""

from dagent.capabilities.bootstrap import create_default_capability_catalog
from dagent.capabilities.catalog import CapabilityCatalog
from dagent.capabilities.decorator import CapabilityBinding, capability, tool
from dagent.capabilities.providers import AgentCapabilityProvider, AgentNodeSessionStore
from dagent.capabilities.toolsets import CapabilityToolAdapter, CapabilityToolset

__all__ = [
    "AgentCapabilityProvider",
    "AgentNodeSessionStore",
    "CapabilityBinding",
    "CapabilityCatalog",
    "CapabilityToolAdapter",
    "CapabilityToolset",
    "capability",
    "create_default_capability_catalog",
    "tool",
]
