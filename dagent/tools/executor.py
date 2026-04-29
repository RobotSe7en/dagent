"""Tool execution with boundary checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dagent.schemas import Boundary
from dagent.tools.boundary import (
    enforce_action_allowed,
    enforce_command_allowed,
    enforce_path_allowed,
    enforce_tool_allowed,
)
from dagent.tools.registry import ToolRegistry


class ToolExecutionError(RuntimeError):
    """Raised when a tool call cannot be executed."""


class ToolExecutor:
    """Executes registered tools through a node boundary."""

    def __init__(self, registry: ToolRegistry, workspace_root: str | Path = ".") -> None:
        self.registry = registry
        self.workspace_root = Path(workspace_root).resolve()

    def execute(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        boundary: Boundary,
    ) -> str:
        tool = self.registry.get(tool_name)
        if tool is None:
            raise ToolExecutionError(f"Tool '{tool_name}' is not registered.")

        enforce_tool_allowed(tool_name, boundary)
        enforce_action_allowed(tool.action, boundary)

        checked_args = {**(tool.default_args or {}), **args}
        for arg_name in tool.path_args:
            if arg_name not in checked_args:
                raise ToolExecutionError(
                    f"Tool '{tool_name}' requires path argument '{arg_name}'."
                )
            checked_args[arg_name] = enforce_path_allowed(
                checked_args[arg_name],
                boundary,
                self.workspace_root,
            )

        for arg_name in tool.command_args:
            if arg_name not in checked_args:
                raise ToolExecutionError(
                    f"Tool '{tool_name}' requires command argument '{arg_name}'."
                )
            enforce_command_allowed(str(checked_args[arg_name]), boundary)

        return tool.handler(**checked_args)

