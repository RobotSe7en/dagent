"""Capability registration and providers."""

from dagent.capabilities.bootstrap import create_default_capability_catalog
from dagent.capabilities.catalog import CapabilityCatalog
from dagent.capabilities.toolsets import CapabilityToolAdapter, CapabilityToolset

__all__ = [
    "CapabilityCatalog",
    "CapabilityToolAdapter",
    "CapabilityToolset",
    "create_default_capability_catalog",
]
