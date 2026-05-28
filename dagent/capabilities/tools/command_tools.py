"""Command-line tools for bounded execution."""

from __future__ import annotations

import subprocess
from pathlib import Path

from dagent.schemas import Boundary
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


def _infer_command_boundary(args: dict) -> Boundary:
    cwd = str(args.get("cwd") or ".")
    return Boundary(mode="write_limited", allowed_paths=[cwd])


def register_command_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="run_command",
        handler=run_command,
        action="command",
        path_args=("cwd",),
        command_args=("command",),
        risk="high",
        boundary_fn=_infer_command_boundary,
        default_args={"cwd": ".", "timeout_seconds": 30},
        description=(
            "Run a shell command in a bounded working directory. "
            "Commands use the system shell and are allowed except hard-blocked dangerous patterns."
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
