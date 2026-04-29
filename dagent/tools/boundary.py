"""Boundary enforcement for tool execution."""

from __future__ import annotations

from pathlib import Path
import re

from dagent.schemas import Boundary


class BoundaryViolation(PermissionError):
    """Raised when a tool call exceeds the node boundary."""


WRITE_ACTIONS = {"write"}
COMMAND_ACTIONS = {"command"}
COMMAND_CONTROL_PATTERN = re.compile(r"&&|\|\||[;|`]")
DEFAULT_READ_ONLY_COMMANDS = {
    "cat",
    "dir",
    "echo",
    "find",
    "findstr",
    "git",
    "grep",
    "ls",
    "pwd",
    "type",
    "where",
    "whoami",
}


def enforce_tool_allowed(tool_name: str, boundary: Boundary) -> None:
    if tool_name in boundary.forbidden_tools:
        raise BoundaryViolation(f"Tool '{tool_name}' is forbidden by boundary.")


def enforce_action_allowed(action: str, boundary: Boundary) -> None:
    if action in WRITE_ACTIONS and boundary.mode == "read_only":
        raise BoundaryViolation("read_only boundary cannot perform write operations.")


def enforce_command_allowed(command: str, boundary: Boundary) -> None:
    if COMMAND_CONTROL_PATTERN.search(command):
        raise BoundaryViolation("Command contains unsupported shell control operators.")

    executable = _command_executable(command)
    if not executable:
        raise BoundaryViolation("Command cannot be empty.")

    if executable in boundary.forbidden_commands or command in boundary.forbidden_commands:
        raise BoundaryViolation(f"Command '{executable}' is forbidden by boundary.")

    allowed = boundary.allowed_commands or _default_allowed_commands(boundary)
    if not allowed:
        raise BoundaryViolation("Command execution requires boundary.allowed_commands.")

    if not any(command == item or executable == item for item in allowed):
        allowed_display = ", ".join(allowed)
        raise BoundaryViolation(
            f"Command '{executable}' is outside allowed commands: {allowed_display}."
        )


def enforce_path_allowed(path: str | Path, boundary: Boundary, workspace_root: Path) -> Path:
    resolved_path = _resolve_against_workspace(path, workspace_root)
    allowed_roots = boundary.allowed_paths or ["."]
    resolved_roots = [
        _resolve_against_workspace(allowed_path, workspace_root)
        for allowed_path in allowed_roots
    ]

    if not any(_is_relative_to(resolved_path, root) for root in resolved_roots):
        allowed_display = ", ".join(str(root) for root in resolved_roots)
        raise BoundaryViolation(
            f"Path '{resolved_path}' is outside allowed paths: {allowed_display}."
        )

    return resolved_path


def _resolve_against_workspace(path: str | Path, workspace_root: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    return candidate.resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _command_executable(command: str) -> str:
    return command.strip().split(maxsplit=1)[0] if command.strip() else ""


def _default_allowed_commands(boundary: Boundary) -> list[str]:
    if boundary.mode != "read_only":
        return []
    return sorted(DEFAULT_READ_ONLY_COMMANDS)

