"""Boundary enforcement for tool execution."""

from __future__ import annotations

from pathlib import Path
import re

from dagent.schemas import Boundary


class BoundaryViolation(PermissionError):
    """Raised when a tool call exceeds the node boundary."""

    def __init__(
        self,
        message: str,
        *,
        tool_name: str | None = None,
        action: str | None = None,
        path: str | None = None,
        command: str | None = None,
    ) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.action = action
        self.path = path
        self.command = command


WRITE_ACTIONS = {"write"}

_BLOCKED_SHELL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;?\s*:"),
        "fork bomb",
    ),
    (
        re.compile(r"\bsudo\b(?=[^;&|`]*\s-S(?:\s|$))", re.IGNORECASE),
        "sudo password from stdin",
    ),
    (
        re.compile(
            r"\brm\b(?=[^;&|`]*-[^\s;&|`]*r)(?=[^;&|`]*-[^\s;&|`]*f)"
            r"[^;&|`]*(?:\s|^)(?:/|/\*|~|~/|\$HOME|\$\{HOME\})(?:\s|$)",
            re.IGNORECASE,
        ),
        "recursive delete of root or home",
    ),
    (
        re.compile(r"\brm\b[^;&|`]*--no-preserve-root\b", re.IGNORECASE),
        "disable rm root protection",
    ),
    (
        re.compile(r"(?:^|[;&|`\n])\s*(?:sudo\s+)?mkfs(?:\.[\w-]+)?\b", re.IGNORECASE),
        "format filesystem",
    ),
    (
        re.compile(
            r"(?:^|[;&|`\n])\s*(?:sudo\s+)?dd\b(?=[^;&|`]*\bof=/dev/"
            r"(?:sd|hd|vd|xvd|nvme|mmcblk|disk)\w*)",
            re.IGNORECASE,
        ),
        "write raw block device",
    ),
    (
        re.compile(
            r"(?:>|>>)\s*/dev/(?:sd|hd|vd|xvd|nvme|mmcblk|disk)\w*",
            re.IGNORECASE,
        ),
        "redirect to raw block device",
    ),
    (
        re.compile(
            r"(?:^|[;&|`\n])\s*(?:sudo\s+)?(?:shutdown|reboot|halt|poweroff)\b",
            re.IGNORECASE,
        ),
        "system shutdown or reboot",
    ),
    (
        re.compile(
            r"(?:^|[;&|`\n])\s*(?:sudo\s+)?systemctl\s+"
            r"(?:poweroff|reboot|halt|shutdown)\b",
            re.IGNORECASE,
        ),
        "system shutdown or reboot",
    ),
)


def enforce_action_allowed(action: str, boundary: Boundary) -> None:
    if action in WRITE_ACTIONS and boundary.mode == "read_only":
        raise BoundaryViolation(
            "read_only boundary cannot perform write operations.",
            action=action,
        )


def enforce_command_allowed(command: str, _boundary: Boundary) -> None:
    if not command.strip():
        raise BoundaryViolation("Command cannot be empty.", command=command)

    blocked_reason = blocked_shell_command(command)
    if blocked_reason:
        raise BoundaryViolation(
            f"Command is blocked by shell safety policy: {blocked_reason}.",
            command=command,
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
            f"Path '{resolved_path}' is outside allowed paths: {allowed_display}.",
            path=str(path),
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


def blocked_shell_command(command: str) -> str | None:
    for pattern, reason in _BLOCKED_SHELL_PATTERNS:
        if pattern.search(command):
            return reason
    return None
