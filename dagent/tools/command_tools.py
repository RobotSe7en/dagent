"""Command-line tools for bounded execution."""

from __future__ import annotations

import subprocess
from pathlib import Path

from dagent.tools.registry import ToolRegistry


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
        timeout=timeout_seconds,
    )
    output = "\n".join(
        part
        for part in [result.stdout.strip(), result.stderr.strip()]
        if part
    )
    return (
        f"exit_code={result.returncode}\n{output}"
        if output
        else f"exit_code={result.returncode}"
    )


def register_command_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="run_command",
        handler=run_command,
        action="command",
        path_args=("cwd",),
        command_args=("command",),
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
