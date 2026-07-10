"""MCP capability provider."""

from __future__ import annotations

import re
from hashlib import sha1
from typing import Any

from dagent.capabilities.catalog import CapabilityCatalog
from dagent.schemas import CapabilityDefinition, CapabilityPolicy, MCPServerSnapshot

from .config import DEFAULT_MCP_TOOL_TIMEOUT_SECONDS
from .handlers import make_mcp_tool_handler
from .manager import MCPServerManager
from .schema import normalize_mcp_input_schema, normalize_mcp_output_schema

_CAPABILITY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_]+$")
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")


class MCPCapabilityProvider:
    """Registers tools discovered from scoped MCP servers."""

    def __init__(
        self,
        servers: dict[str, dict[str, Any]] | None = None,
        *,
        manager: Any | None = None,
        snapshots: dict[str, MCPServerSnapshot] | None = None,
        lazy_connect: bool = False,
    ) -> None:
        self.servers = servers or {}
        self.manager = manager
        self.snapshots = snapshots or {}
        self.lazy_connect = lazy_connect
        self.registration_errors: list[str] = []

    def register_into(self, catalog: CapabilityCatalog) -> None:
        if not self.servers:
            return
        manager = self.manager or MCPServerManager(self.servers)
        self.manager = manager
        if not getattr(manager, "available", True):
            return
        if self.lazy_connect and self.snapshots:
            for server_name, snapshot in sorted(self.snapshots.items()):
                self._register_snapshot_tools(catalog, server_name, snapshot)
            if hasattr(catalog, "add_shutdown_hook") and hasattr(manager, "shutdown"):
                catalog.add_shutdown_hook(manager.shutdown)
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
        input_schema = getattr(tool, "inputSchema", None)
        if input_schema is None:
            input_schema = getattr(tool, "input_schema", None)
        output_schema = getattr(tool, "outputSchema", None)
        if output_schema is None:
            output_schema = getattr(tool, "output_schema", None)
        server_config = self.servers.get(server_name, {})
        definition = CapabilityDefinition(
            id=capability_id,
            kind="mcp",
            description=str(getattr(tool, "description", "") or ""),
            parameters=normalize_mcp_input_schema(input_schema),
            output_schema=normalize_mcp_output_schema(output_schema),
            policy=CapabilityPolicy(
                risk=str(server_config.get("risk", "medium")),
                sandbox_required=True,
                network=bool(server_config.get("url")),
            ),
            config={"server": server_name, "tool": tool_name},
        )
        handler = make_mcp_tool_handler(
            self.manager,
            server_name=server_name,
            tool_name=tool_name,
            timeout_seconds=float(
                server_config.get("tool_timeout", DEFAULT_MCP_TOOL_TIMEOUT_SECONDS)
            ),
        )
        try:
            catalog.register(definition, handler)
        except ValueError as exc:
            self.registration_errors.append(str(exc))

    def _register_snapshot_tools(
        self,
        catalog: CapabilityCatalog,
        server_name: str,
        snapshot: MCPServerSnapshot,
    ) -> None:
        if snapshot.name != server_name:
            raise ValueError(
                f"MCP snapshot name '{snapshot.name}' does not match server '{server_name}'."
            )
        for tool in snapshot.tools:
            if tool.server != server_name:
                raise ValueError(
                    f"MCP snapshot tool '{tool.capability_id}' has server '{tool.server}', "
                    f"expected '{server_name}'."
                )
            definition = tool.definition.model_copy(
                update={
                    "config": {
                        **tool.definition.config,
                        "server": server_name,
                        "tool": tool.tool,
                    }
                },
                deep=True,
            )
            handler = make_mcp_tool_handler(
                self.manager,
                server_name=server_name,
                tool_name=tool.tool,
                timeout_seconds=float(
                    self.servers.get(server_name, {}).get(
                        "tool_timeout",
                        DEFAULT_MCP_TOOL_TIMEOUT_SECONDS,
                    )
                ),
            )
            try:
                catalog.register(definition, handler)
            except ValueError as exc:
                self.registration_errors.append(str(exc))


def _safe_component(value: str) -> str:
    raw = value.strip()
    if _CAPABILITY_SEGMENT_RE.fullmatch(raw):
        return raw
    slug = _UNSAFE_NAME_RE.sub("_", raw).strip("_").lower() or "unnamed"
    return f"{slug}_{_short_hash(raw)}"


def _short_hash(value: str) -> str:
    return sha1(value.encode("utf-8")).hexdigest()[:8]


__all__ = [
    "MCPCapabilityProvider",
    "MCPServerManager",
    "normalize_mcp_input_schema",
    "normalize_mcp_output_schema",
]
