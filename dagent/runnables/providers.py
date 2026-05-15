"""Built-in runnable providers."""

from __future__ import annotations

import subprocess
import asyncio
import threading
from pathlib import Path
from typing import Any

import yaml

from dagent.providers import ChatProvider
from dagent.runnables.executor import RunnableExecutor
from dagent.runnables.registry import RunnableRegistry
from dagent.runnables.schemas import (
    RunnableDefinition,
    RunnableInvocation,
    RunnablePolicy,
    RunnableResult,
)
from dagent.schemas import Boundary
from dagent.tools.boundary import (
    enforce_action_allowed,
    enforce_command_allowed,
    enforce_path_allowed,
)
from dagent.tools.registry import ToolRegistry


class ToolRunnableProvider:
    """Exposes existing ToolRegistry entries as tool runnables."""

    def __init__(self, tools: ToolRegistry) -> None:
        self.tools = tools

    def register_into(self, registry: RunnableRegistry, executor: RunnableExecutor) -> None:
        for name in sorted(self.tools.names()):
            tool = self.tools.get(name)
            if tool is None:
                continue
            runnable_id = f"tool.{name}"
            registry.register(
                RunnableDefinition(
                    id=runnable_id,
                    name=name,
                    kind="tool",
                    description=tool.description,
                    parameters=tool.parameters or {"type": "object"},
                    policy=RunnablePolicy(risk=tool.risk),
                    config={"tool_name": name},
                )
            )

            def handler(invocation: RunnableInvocation, *, tool_name: str = name) -> RunnableResult:
                try:
                    content = _execute_tool(self.tools, executor.workspace_root, tool_name, invocation)
                    return _completed(invocation, content)
                except Exception as exc:
                    return _failed(invocation, str(exc), stop_reason=type(exc).__name__)

            executor.register_handler(runnable_id, handler)


class ShellRunnableProvider:
    """Registers allowlisted shell commands as runnable capabilities."""

    def __init__(self, commands: dict[str, dict[str, Any]]) -> None:
        self.commands = commands

    def register_into(self, registry: RunnableRegistry, executor: RunnableExecutor) -> None:
        for name, config in sorted(self.commands.items()):
            runnable_id = f"shell.{name}"
            command = str(config["command"])
            registry.register(
                RunnableDefinition(
                    id=runnable_id,
                    name=name,
                    kind="shell",
                    description=str(config.get("description", "")),
                    parameters=config.get("parameters") or {"type": "object"},
                    policy=RunnablePolicy(risk="high", sandbox_required=True),
                    config={"command": command},
                )
            )

            def handler(invocation: RunnableInvocation, *, command_template: str = command) -> RunnableResult:
                try:
                    enforce_command_allowed(command_template, invocation.boundary)
                    completed = subprocess.run(
                        command_template,
                        cwd=executor.workspace_root,
                        capture_output=True,
                        text=True,
                        shell=True,
                        timeout=float(invocation.arguments.get("timeout_seconds", 30)),
                    )
                except Exception as exc:
                    return _failed(invocation, str(exc), stop_reason=type(exc).__name__)
                status = "completed" if completed.returncode == 0 else "failed"
                content = completed.stdout or completed.stderr
                return RunnableResult(
                    invocation_id=invocation.invocation_id,
                    runnable_id=invocation.runnable_id,
                    kind=invocation.kind,
                    status=status,
                    content=content,
                    error=None if status == "completed" else completed.stderr,
                    stop_reason="completed" if status == "completed" else "nonzero_exit",
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    policy_decision=_policy_decision(invocation.boundary),
                )

            executor.register_handler(runnable_id, handler)


class MemoryRunnableProvider:
    """In-memory scoped key/value memory runnables."""

    def __init__(self) -> None:
        self._memory: dict[str, dict[str, str]] = {}

    def register_into(self, registry: RunnableRegistry, executor: RunnableExecutor) -> None:
        registry.register(RunnableDefinition(id="memory.write", name="memory_write", kind="memory"))
        registry.register(RunnableDefinition(id="memory.search", name="memory_search", kind="memory"))
        executor.register_handler("memory.write", self._write)
        executor.register_handler("memory.search", self._search)

    def _write(self, invocation: RunnableInvocation) -> RunnableResult:
        scope = str(invocation.arguments.get("scope", "session"))
        key = str(invocation.arguments["key"])
        value = str(invocation.arguments["value"])
        self._memory.setdefault(scope, {})[key] = value
        return _completed(invocation, f"{key}={value}")

    def _search(self, invocation: RunnableInvocation) -> RunnableResult:
        scope = str(invocation.arguments.get("scope", "session"))
        query = str(invocation.arguments.get("query", ""))
        values = self._memory.get(scope, {})
        matches = [
            f"{key}={value}"
            for key, value in sorted(values.items())
            if query.lower() in key.lower() or query.lower() in value.lower()
        ]
        return _completed(invocation, "\n".join(matches))


class FileRunnableProvider:
    """Workspace-scoped file runnables."""

    def register_into(self, registry: RunnableRegistry, executor: RunnableExecutor) -> None:
        registry.register(RunnableDefinition(id="file.read", name="file_read", kind="file"))
        registry.register(RunnableDefinition(id="file.write", name="file_write", kind="file", policy=RunnablePolicy(risk="medium")))

        def read(invocation: RunnableInvocation) -> RunnableResult:
            try:
                path = enforce_path_allowed(str(invocation.arguments["path"]), invocation.boundary, executor.workspace_root)
                return _completed(invocation, path.read_text(encoding="utf-8"))
            except Exception as exc:
                return _failed(invocation, str(exc), stop_reason=type(exc).__name__)

        def write(invocation: RunnableInvocation) -> RunnableResult:
            try:
                enforce_action_allowed("write", invocation.boundary)
                path = enforce_path_allowed(str(invocation.arguments["path"]), invocation.boundary, executor.workspace_root)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(invocation.arguments.get("content", "")), encoding="utf-8")
                return _completed(invocation, f"wrote:{path}")
            except Exception as exc:
                return _failed(invocation, str(exc), stop_reason=type(exc).__name__)

        executor.register_handler("file.read", read)
        executor.register_handler("file.write", write)


class MCPRunnableProvider:
    """Registers configured MCP-like tools as runnable capabilities.

    The first implementation accepts already-discovered tool handlers. Real
    stdio/http discovery can populate the same shape before registration.
    """

    def __init__(self, servers: dict[str, dict[str, Any]]) -> None:
        self.servers = servers

    def register_into(self, registry: RunnableRegistry, executor: RunnableExecutor) -> None:
        for server_name, server_config in sorted(self.servers.items()):
            tools = server_config.get("tools", {})
            for tool_name, tool_config in sorted(tools.items()):
                runnable_id = f"mcp.{server_name}.{tool_name}"
                handler = tool_config.get("handler")
                registry.register(
                    RunnableDefinition(
                        id=runnable_id,
                        name=f"{server_name}.{tool_name}",
                        kind="mcp",
                        description=str(tool_config.get("description", "")),
                        parameters=tool_config.get("parameters") or {"type": "object"},
                        policy=RunnablePolicy(risk=tool_config.get("risk", "medium"), sandbox_required=True),
                        config={"server": server_name, "tool": tool_name},
                    )
                )

                def execute(invocation: RunnableInvocation, *, runnable_handler=handler) -> RunnableResult:
                    if not callable(runnable_handler):
                        return _failed(invocation, "MCP tool has no callable handler.", stop_reason="missing_handler")
                    try:
                        return _completed(invocation, str(runnable_handler(**invocation.arguments)))
                    except Exception as exc:
                        return _failed(invocation, str(exc), stop_reason=type(exc).__name__)

                executor.register_handler(runnable_id, execute)


class SkillRunnableProvider:
    """Registers local skills as runnable capabilities."""

    def __init__(self, skill_roots: list[str | Path]) -> None:
        self.skill_roots = [Path(root) for root in skill_roots]

    def register_into(self, registry: RunnableRegistry, executor: RunnableExecutor) -> None:
        for skill_file in self._skill_files():
            metadata, body = _read_skill(skill_file)
            name = str(metadata.get("name") or skill_file.parent.name)
            runnable_id = f"skill.{name}"
            registry.register(
                RunnableDefinition(
                    id=runnable_id,
                    name=name,
                    kind="skill",
                    description=str(metadata.get("description", "")),
                    policy=RunnablePolicy(risk="medium", sandbox_required=True),
                    config={"path": str(skill_file)},
                )
            )

            def execute(invocation: RunnableInvocation, *, skill_body: str = body) -> RunnableResult:
                user_input = str(invocation.arguments.get("input", ""))
                content = skill_body if not user_input else f"{skill_body}\n\nInput:\n{user_input}"
                return _completed(invocation, content)

            executor.register_handler(runnable_id, execute)

    def _skill_files(self) -> list[Path]:
        files: list[Path] = []
        for root in self.skill_roots:
            if root.is_file() and root.name == "SKILL.md":
                files.append(root)
            elif root.exists():
                files.extend(root.glob("*/SKILL.md"))
        return sorted(files)


class CustomToolRunnableProvider:
    """Registers web/user-defined custom tool handlers."""

    def __init__(self, tools: dict[str, dict[str, Any]]) -> None:
        self.tools = tools

    def register_into(self, registry: RunnableRegistry, executor: RunnableExecutor) -> None:
        for name, config in sorted(self.tools.items()):
            runnable_id = f"custom_tool.{name}"
            handler = config.get("handler")
            registry.register(
                RunnableDefinition(
                    id=runnable_id,
                    name=name,
                    kind="custom_tool",
                    description=str(config.get("description", "")),
                    parameters=config.get("parameters") or {"type": "object"},
                    policy=RunnablePolicy(risk=config.get("risk", "medium"), sandbox_required=True),
                    config={key: value for key, value in config.items() if key != "handler"},
                )
            )

            def execute(invocation: RunnableInvocation, *, runnable_handler=handler) -> RunnableResult:
                if not callable(runnable_handler):
                    return _failed(invocation, "Custom tool has no callable handler.", stop_reason="missing_handler")
                try:
                    return _completed(invocation, str(runnable_handler(**invocation.arguments)))
                except Exception as exc:
                    return _failed(invocation, str(exc), stop_reason=type(exc).__name__)

            executor.register_handler(runnable_id, execute)


class AgentRunnableProvider:
    """Registers agent templates as runnable capabilities."""

    def __init__(self, agents: dict[str, dict[str, Any]]) -> None:
        self.agents = agents
        self._messages: dict[str, list[dict[str, str]]] = {}

    def register_into(self, registry: RunnableRegistry, executor: RunnableExecutor) -> None:
        for name, config in sorted(self.agents.items()):
            runnable_id = f"agent.{name}"
            provider = config["provider"]
            if not hasattr(provider, "chat"):
                raise TypeError(f"Agent runnable '{name}' requires a chat provider.")
            registry.register(
                RunnableDefinition(
                    id=runnable_id,
                    name=name,
                    kind="agent",
                    description=str(config.get("description", "")),
                    parameters=config.get("parameters") or {"type": "object"},
                    policy=RunnablePolicy(risk=config.get("risk", "medium"), sandbox_required=True),
                    config={"system": str(config.get("system", ""))},
                )
            )

            def execute(
                invocation: RunnableInvocation,
                *,
                agent_name: str = name,
                agent_provider: ChatProvider = provider,
                system_prompt: str = str(config.get("system", "")),
            ) -> RunnableResult:
                messages = self._messages.setdefault(
                    agent_name,
                    [{"role": "system", "content": system_prompt}],
                )
                messages.append({"role": "user", "content": str(invocation.arguments.get("prompt", ""))})
                try:
                    response = _run_chat_sync(agent_provider, messages)
                except Exception as exc:
                    return _failed(invocation, str(exc), stop_reason=type(exc).__name__)
                messages.append({"role": "assistant", "content": response.content})
                return _completed(invocation, response.content)

            executor.register_handler(runnable_id, execute)


def _execute_tool(
    tools: ToolRegistry,
    workspace_root: Path,
    tool_name: str,
    invocation: RunnableInvocation,
) -> str:
    tool = tools.get(tool_name)
    if tool is None:
        raise RuntimeError(f"Tool '{tool_name}' is not registered.")
    enforce_action_allowed(tool.action, invocation.boundary)
    checked_args = {**(tool.default_args or {}), **invocation.arguments}
    for arg_name in tool.path_args:
        checked_args[arg_name] = enforce_path_allowed(
            checked_args[arg_name],
            invocation.boundary,
            workspace_root,
        )
    for arg_name in tool.command_args:
        enforce_command_allowed(str(checked_args[arg_name]), invocation.boundary)
    return tool.handler(**checked_args)


def _read_skill(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            metadata = yaml.safe_load(parts[1]) or {}
            return metadata if isinstance(metadata, dict) else {}, parts[2].strip()
    return {}, text.strip()


def _run_chat_sync(provider: ChatProvider, messages: list[dict[str, str]]):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(provider.chat(messages))
    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["response"] = asyncio.run(provider.chat(messages))
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result["response"]


def _completed(invocation: RunnableInvocation, content: str) -> RunnableResult:
    return RunnableResult(
        invocation_id=invocation.invocation_id,
        runnable_id=invocation.runnable_id,
        kind=invocation.kind,
        status="completed",
        content=content,
        policy_decision=_policy_decision(invocation.boundary),
    )


def _failed(invocation: RunnableInvocation, error: str, *, stop_reason: str) -> RunnableResult:
    return RunnableResult(
        invocation_id=invocation.invocation_id,
        runnable_id=invocation.runnable_id,
        kind=invocation.kind,
        status="failed",
        error=error,
        stop_reason=stop_reason,
        policy_decision=_policy_decision(invocation.boundary),
    )


def _policy_decision(boundary: Boundary) -> dict[str, Any]:
    return {
        "boundary_mode": boundary.mode,
        "allowed_paths": boundary.allowed_paths or [],
        "allowed_commands": boundary.allowed_commands or [],
    }
