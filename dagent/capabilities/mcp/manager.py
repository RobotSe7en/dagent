"""Scoped MCP server manager."""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import threading
from typing import Any

from .config import DEFAULT_MCP_CONNECT_TIMEOUT_SECONDS
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
            connect_timeout = _connect_timeout_seconds(self.servers[name])
            try:
                future.result(timeout=connect_timeout)
            except FutureTimeoutError:
                self.last_errors[name] = f"timed out after {_format_timeout(connect_timeout)} seconds."
                future.cancel()
                self._discard_task(name)
            except Exception as exc:
                self.last_errors[name] = str(exc)
                future.cancel()
                self._discard_task(name)
        if self.tasks:
            self._started = True
        else:
            self._stop_loop()

    def discovered_tools(self) -> dict[str, list[Any]]:
        return {name: task.tools for name, task in sorted(self.tasks.items()) if not task.last_error}

    def ensure_started(self, name: str) -> None:
        if not self.available:
            return
        if name in self.tasks:
            return
        if name not in self.servers:
            raise RuntimeError(f"MCP server '{name}' is not configured.")
        if self.servers[name].get("enabled", True) is False:
            raise RuntimeError(f"MCP server '{name}' is disabled.")
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run_loop, name="dagent-mcp-manager", daemon=True)
            self._thread.start()
        task = MCPServerTask(name, self.servers[name])
        self.tasks[name] = task
        future = asyncio.run_coroutine_threadsafe(task.start(), self._loop)
        connect_timeout = _connect_timeout_seconds(self.servers[name])
        try:
            future.result(timeout=connect_timeout)
        except FutureTimeoutError:
            self.last_errors[name] = f"timed out after {_format_timeout(connect_timeout)} seconds."
            future.cancel()
            self._discard_task(name)
            if not self.tasks:
                self._stop_loop()
            raise RuntimeError(f"MCP server '{name}' failed to connect: {self.last_errors[name]}") from None
        except Exception as exc:
            self.last_errors[name] = str(exc)
            future.cancel()
            self._discard_task(name)
            if not self.tasks:
                self._stop_loop()
            raise RuntimeError(f"MCP server '{name}' failed to connect: {exc}") from exc
        self._started = True

    def call_tool_blocking(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> Any:
        self.ensure_started(server_name)
        if self._loop is None:
            raise RuntimeError("MCP manager is not started.")
        task = self.tasks.get(server_name)
        if task is None:
            raise RuntimeError(f"MCP server '{server_name}' is not connected.")
        future = asyncio.run_coroutine_threadsafe(task.call_tool(tool_name, arguments), self._loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise FutureTimeoutError(
                f"MCP tool '{tool_name}' on server '{server_name}' timed out "
                f"after {_format_timeout(timeout)} seconds."
            ) from None

    def shutdown(self) -> None:
        if self._loop is None:
            return
        futures = [asyncio.run_coroutine_threadsafe(task.shutdown(), self._loop) for task in self.tasks.values()]
        for future in futures:
            try:
                future.result(timeout=10)
            except FutureTimeoutError:
                future.cancel()
        self.tasks.clear()
        self._stop_loop()

    def _discard_task(self, name: str) -> None:
        task = self.tasks.pop(name, None)
        if task is None or self._loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(task.shutdown(), self._loop)
        try:
            future.result(timeout=10)
        except FutureTimeoutError:
            future.cancel()
        except Exception as exc:
            self.last_errors[name] = f"{self.last_errors.get(name, '')}; shutdown failed: {exc}".strip("; ")

    def _stop_loop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._loop is not None and not self._loop.is_closed():
            self._loop.close()
        self._loop = None
        self._thread = None
        self._started = False

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()


def _connect_timeout_seconds(config: dict[str, Any]) -> float:
    return float(config.get("connect_timeout", DEFAULT_MCP_CONNECT_TIMEOUT_SECONDS))


def _format_timeout(timeout: float) -> str:
    return f"{timeout:g}"
