"""Default capability catalog assembly."""

from __future__ import annotations

from pathlib import Path

from dagent.capabilities.catalog import CapabilityCatalog
from dagent.capabilities.providers import (
    FileCapabilityProvider,
    MemoryCapabilityProvider,
    ToolCapabilityProvider,
)
from dagent.capabilities.mcp import MCPCapabilityProvider
from dagent.capabilities.skills import SkillsCapabilityProvider
from dagent.capabilities.tools.file_tools import create_file_tool_registry


def create_default_capability_catalog(
    *,
    workspace_root: str | Path = ".",
    skill_roots: list[str | Path] | None = None,
    skills_provider: SkillsCapabilityProvider | None = None,
    mcp_servers: dict[str, dict] | None = None,
) -> CapabilityCatalog:
    catalog = CapabilityCatalog(workspace_root=workspace_root)
    ToolCapabilityProvider(create_file_tool_registry()).register_into(catalog)
    MemoryCapabilityProvider().register_into(catalog)
    FileCapabilityProvider().register_into(catalog)
    (skills_provider or SkillsCapabilityProvider(skill_roots)).register_into(catalog)
    if mcp_servers:
        MCPCapabilityProvider(mcp_servers).register_into(catalog)
    return catalog
