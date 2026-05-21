"""Command-line tools for bounded execution."""

from __future__ import annotations

import subprocess
from pathlib import Path

from dagent.schemas import Boundary
from dagent.capabilities.tools.boundary import DEFAULT_READ_ONLY_COMMANDS
from dagent.capabilities.tools.registry import ToolRegistry


class CommandExecutionError(RuntimeError):
    """Raised when a command tool exits unsuccessfully."""


def run_command(
    command: str,
    cwd: str | Path = ".",
    timeout_seconds: int = 30,
) -> str:
    result = subprocess.run(
        command,
        cwd=Path(cwd),
        shell=True,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout_seconds,
    )
    output = "\n".join(
        part
        for part in [result.stdout.strip(), result.stderr.strip()]
        if part
    )
    formatted = (
        f"exit_code={result.returncode}\n{output}"
        if output
        else f"exit_code={result.returncode}"
    )
    if result.returncode != 0:
        raise CommandExecutionError(formatted)
    return formatted


def _command_executable(command: str) -> str:
    return command.strip().split(maxsplit=1)[0] if command.strip() else ""


def _infer_command_boundary(args: dict) -> Boundary:
    """Infer boundary from command arguments.

    Read-only commands (ls, cat, git, etc.) get read_only boundary.
    All other commands get write_limited and require approval.
    """
    command = str(args.get("command") or "").strip()
    executable = _command_executable(command)
    cwd = str(args.get("cwd") or ".")
    is_read_only = executable in DEFAULT_READ_ONLY_COMMANDS
    return Boundary(
        mode="read_only" if is_read_only else "write_limited",
        allowed_paths=[cwd],
        allowed_commands=[] if is_read_only else [executable or command],
    )


def _infer_command_risk(args: dict) -> str:
    """Infer risk from command arguments.

    Whitelisted read-only commands are low risk.
    All other commands are high risk.
    """
    command = str(args.get("command") or "").strip()
    executable = _command_executable(command)
    if executable in DEFAULT_READ_ONLY_COMMANDS:
        return "low"
    return "high"


def register_command_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="run_command",
        handler=run_command,
        action="command",
        path_args=("cwd",),
        command_args=("command",),
        risk="low",
        boundary_fn=_infer_command_boundary,
        risk_fn=_infer_command_risk,
        default_args={"cwd": ".", "timeout_seconds": 30},
        description=(
            "Run a bounded command in a bounded working directory. "
            "Read-only common inspection commands are allowed by default; other commands require boundary.allowed_commands."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command line to run."},
                "cwd": {
                    "type": "string",
                    "description": "Working directory relative to the workspace.",
                    "default": ".",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Maximum runtime in seconds.",
                    "default": 30,
                },
            },
            "required": ["command"],
        },
    )
