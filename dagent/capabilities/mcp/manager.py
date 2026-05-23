"""Scoped MCP server manager."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from .server_task import MCPServerTask, MCP_SDK_AVAILABLE


class MCPServerManager:
    """Runtime/workspace scoped MCP manager with a private event loop."""

    available = MCP_SDK_AVAILABLE

    def __init__(self, servers: dict[str, dict[str, Any]]) -> None:
        self.servers = servers
        self.tasks: dict[str, MCPServerTask] = {}
        self.last_errors: dict[str, str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        if self._started or not self.available:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="dagent-mcp-manager", daemon=True)
        self._thread.start()
        futures = []
        for name, config in sorted(self.servers.items()):
            if config.get("enabled", True) is False:
                continue
            task = MCPServerTask(name, config)
            self.tasks[name] = task
            futures.append((name, asyncio.run_coroutine_threadsafe(task.start(), self._loop)))
        for name, future in futures:
            try:
                future.result(timeout=float(self.servers[name].get("connect_timeout", 30)))
            except Exception as exc:
                self.last_errors[name] = str(exc)
        self._started = True

    def discovered_tools(self) -> dict[str, list[Any]]:
        return {name: task.tools for name, task in sorted(self.tasks.items()) if not task.last_error}

    def call_tool_blocking(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> Any:
        if self._loop is None:
            raise RuntimeError("MCP manager is not started.")
        task = self.tasks.get(server_name)
        if task is None:
            raise RuntimeError(f"MCP server '{server_name}' is not connected.")
        future = asyncio.run_coroutine_threadsafe(task.call_tool(tool_name, arguments), self._loop)
        return future.result(timeout=timeout)

    def shutdown(self) -> None:
        if self._loop is None:
            return
        futures = [asyncio.run_coroutine_threadsafe(task.shutdown(), self._loop) for task in self.tasks.values()]
        for future in futures:
            future.result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._started = False

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
