"""Async MCP server task."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_MCP_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_MCP_TOOL_TIMEOUT_SECONDS,
    build_http_headers,
    build_stdio_env,
)

try:  # pragma: no cover - optional dependency is installed by dev and mcp extras
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

try:  # pragma: no cover - exercised in integration environments with mcp installed
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    MCP_SDK_AVAILABLE = True
except Exception:  # pragma: no cover - import depends on optional extra
    ClientSession = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    streamable_http_client = None  # type: ignore[assignment]
    create_mcp_http_client = None  # type: ignore[assignment]
    MCP_SDK_AVAILABLE = False


class MCPServerTask:
    """Owns one MCP server connection and serializes JSON-RPC requests."""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.config = config
        self.tools: list[Any] = []
        self.initialize_result: Any = None
        self.last_error: str | None = None
        self._session: Any = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._rpc_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not MCP_SDK_AVAILABLE:
            raise RuntimeError("MCP SDK is not installed. Install dagent[mcp] to enable MCP servers.")
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name=f"mcp:{self.name}")
        await self._ready.wait()
        if self.last_error:
            raise RuntimeError(self.last_error)

    async def run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                read_stream, write_stream = await self._open_streams(stack)
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                self._session = session
                self.initialize_result = await session.initialize()
                async with self._rpc_lock:
                    tool_result = await session.list_tools()
                self.tools = list(getattr(tool_result, "tools", []) or [])
                self._ready.set()
                await self._stop.wait()
        except Exception as exc:
            self.last_error = str(exc)
            self._ready.set()
        finally:
            self._session = None

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if self._session is None:
            raise RuntimeError(f"MCP server '{self.name}' is not connected.")
        async with self._rpc_lock:
            return await self._session.call_tool(tool_name, arguments=arguments)

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    async def _open_streams(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        transport = self._transport()
        if transport == "stdio":
            stderr_file = _stderr_log_path().open("a", encoding="utf-8")
            stack.callback(stderr_file.close)
            return await stack.enter_async_context(
                stdio_client(self._stdio_params(), errlog=stderr_file)
            )
        http_client = await stack.enter_async_context(create_mcp_http_client(**self._http_client_params()))
        read_stream, write_stream, _get_session_id = await stack.enter_async_context(
            streamable_http_client(self._http_url(), http_client=http_client)
        )
        return read_stream, write_stream

    def _transport(self) -> str:
        raw_transport = self.config.get("transport")
        if raw_transport is None:
            if self.config.get("url"):
                raise ValueError(f"MCP server '{self.name}' has url but is missing transport: http.")
            return "stdio"
        if not isinstance(raw_transport, str) or raw_transport not in {"stdio", "http"}:
            raise ValueError(f"MCP server '{self.name}' has unknown transport '{raw_transport}'.")
        return raw_transport

    def _stdio_params(self) -> Any:
        command = str(self.config.get("command") or "")
        if not command:
            raise ValueError(f"MCP server '{self.name}' is missing command.")
        return StdioServerParameters(
            command=command,
            args=[str(arg) for arg in self.config.get("args", [])],
            env=build_stdio_env(self.config.get("env") or {}),
            cwd=self.config.get("cwd"),
        )

    def _http_url(self) -> str:
        url = str(self.config.get("url") or "").strip()
        if not url:
            raise ValueError(f"MCP server '{self.name}' is missing url.")
        return url

    def _http_client_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "headers": build_http_headers(self.config.get("headers") or {}),
        }
        connect_timeout = self.config.get("connect_timeout")
        tool_timeout = self.config.get("tool_timeout")
        connect = float(
            connect_timeout
            if connect_timeout is not None
            else DEFAULT_MCP_CONNECT_TIMEOUT_SECONDS
        )
        read = float(
            tool_timeout
            if tool_timeout is not None
            else DEFAULT_MCP_TOOL_TIMEOUT_SECONDS
        )
        params["timeout"] = httpx.Timeout(connect, read=read)
        return params


def _stderr_log_path() -> Path:
    path = Path.home() / ".dagent" / "logs" / "mcp-stderr.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
