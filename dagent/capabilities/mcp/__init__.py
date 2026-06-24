"""MCP capability provider."""

from __future__ import annotations

import re
from typing import Any

from dagent.capabilities.catalog import CapabilityCatalog
from dagent.schemas import CapabilityDefinition, CapabilityPolicy

from .handlers import make_mcp_tool_handler
from .manager import MCPServerManager
from .schema import normalize_mcp_input_schema

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")


class MCPCapabilityProvider:
    """Registers tools discovered from scoped MCP servers."""

    def __init__(
        self,
        servers: dict[str, dict[str, Any]] | None = None,
        *,
        manager: Any | None = None,
    ) -> None:
        self.servers = servers or {}
        self.manager = manager
        self.registration_errors: list[str] = []

    def register_into(self, catalog: CapabilityCatalog) -> None:
        if not self.servers:
            return
        manager = self.manager or MCPServerManager(self.servers)
        self.manager = manager
        if not getattr(manager, "available", True):
            return
        manager.start()
        if hasattr(catalog, "add_shutdown_hook") and hasattr(manager, "shutdown"):
            catalog.add_shutdown_hook(manager.shutdown)
        for server_name, tools in sorted(manager.discovered_tools().items()):
            include = set(self.servers.get(server_name, {}).get("include_tools") or [])
            exclude = set(self.servers.get(server_name, {}).get("exclude_tools") or [])
            for tool in sorted(tools, key=lambda item: str(getattr(item, "name", ""))):
                tool_name = str(getattr(tool, "name", ""))
                if not tool_name:
                    continue
                if include and tool_name not in include:
                    continue
                if tool_name in exclude:
                    continue
                self._register_tool(catalog, server_name, tool_name, tool)

    def _register_tool(
        self,
        catalog: CapabilityCatalog,
        server_name: str,
        tool_name: str,
        tool: Any,
    ) -> None:
        safe_server = _safe_component(server_name)
        safe_tool = _safe_component(tool_name)
        capability_id = f"mcp.{safe_server}.{safe_tool}"
        if catalog.get(capability_id) is not None:
            self.registration_errors.append(
                f"MCP tool '{server_name}.{tool_name}' collides with existing capability '{capability_id}'."
            )
            return
        input_schema = getattr(tool, "inputSchema", None)
        if input_schema is None:
            input_schema = getattr(tool, "input_schema", None)
        server_config = self.servers.get(server_name, {})
        definition = CapabilityDefinition(
            id=capability_id,
            name=safe_tool,
            kind="mcp",
            description=str(getattr(tool, "description", "") or ""),
            parameters=normalize_mcp_input_schema(input_schema),
            policy=CapabilityPolicy(
                risk=str(server_config.get("risk", "medium")),
                sandbox_required=True,
                network=bool(server_config.get("url")),
            ),
            config={"server": server_name, "tool": tool_name},
        )
        catalog.register(
            definition,
            make_mcp_tool_handler(
                self.manager,
                server_name=server_name,
                tool_name=tool_name,
                timeout_seconds=float(server_config.get("tool_timeout", 60)),
            ),
        )


def _safe_component(value: str) -> str:
    safe = _SAFE_NAME_RE.sub("_", value.strip()).strip("_").lower()
    return safe or "unnamed"


__all__ = ["MCPCapabilityProvider", "MCPServerManager", "normalize_mcp_input_schema"]
