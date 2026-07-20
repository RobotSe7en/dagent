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
        self._start_lock = threading.RLock()

    def start(self) -> None:
        with self._start_lock:
            self._start_all_locked()

    def _start_all_locked(self) -> None:
        if self._started or not self.available:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="dagent-mcp-manager",
            daemon=True,
        )
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
        with self._start_lock:
            self._ensure_started_locked(name)

    def _ensure_started_locked(self, name: str) -> None:
        if name in self.tasks:
            return
        if name not in self.servers:
            raise RuntimeError(f"MCP server '{name}' is not configured.")
        if self.servers[name].get("enabled", True) is False:
            raise RuntimeError(f"MCP server '{name}' is disabled.")
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="dagent-mcp-manager",
                daemon=True,
            )
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

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> Any:
        """Call an MCP tool while propagating caller cancellation to its event loop."""

        await asyncio.to_thread(self.ensure_started, server_name)
        if self._loop is None:
            raise RuntimeError("MCP manager is not started.")
        task = self.tasks.get(server_name)
        if task is None:
            raise RuntimeError(f"MCP server '{server_name}' is not connected.")
        future = asyncio.run_coroutine_threadsafe(
            task.call_tool(tool_name, arguments),
            self._loop,
        )
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(future),
                timeout=timeout,
            )
        except TimeoutError:
            future.cancel()
            raise FutureTimeoutError(
                f"MCP tool '{tool_name}' on server '{server_name}' timed out "
                f"after {_format_timeout(timeout)} seconds."
            ) from None
        except asyncio.CancelledError:
            future.cancel()
            raise

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
        loop = self._loop
        thread = self._thread
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError(
                    "MCP manager thread did not stop within 10 seconds."
                )
        if loop is not None and not loop.is_closed():
            loop.close()
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
