"""Capability registration and providers."""

from dagent.capabilities.bootstrap import create_default_capability_catalog
from dagent.capabilities.catalog import CapabilityCatalog

__all__ = ["CapabilityCatalog", "create_default_capability_catalog"]
