"""Command-line tools for bounded execution."""

from __future__ import annotations

import subprocess
from pathlib import Path

from dagent.capabilities.tools.registry import ToolRegistry


COMMAND_OUTPUT_MAX_LINES = 200
COMMAND_OUTPUT_MAX_BYTES = 100_000


class CommandExecutionError(RuntimeError):
    """Raised when a command tool exits unsuccessfully."""


def run_command(
    command: str,
    cwd: str | Path = ".",
    timeout_seconds: int = 30,
) -> str:
    cwd_path = Path(cwd)
    if not cwd_path.is_dir():
        raise CommandExecutionError(f"Working directory does not exist: {cwd_path}")
    result = subprocess.run(
        command,
        cwd=cwd_path,
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
    output = _tail_truncate(output)
    formatted = (
        f"exit_code={result.returncode}\n{output}"
        if output
        else f"exit_code={result.returncode}"
    )
    if result.returncode != 0:
        raise CommandExecutionError(formatted)
    return formatted


def _tail_truncate(output: str) -> str:
    """Keep the tail of oversized output; command endings carry the signal."""
    lines = output.splitlines()
    truncated = False
    if len(lines) > COMMAND_OUTPUT_MAX_LINES:
        lines = lines[-COMMAND_OUTPUT_MAX_LINES:]
        truncated = True
    text = "\n".join(lines)
    encoded = text.encode("utf-8")
    if len(encoded) > COMMAND_OUTPUT_MAX_BYTES:
        text = encoded[-COMMAND_OUTPUT_MAX_BYTES:].decode("utf-8", errors="replace")
        truncated = True
    if truncated:
        text = f"[TRUNCATED: output exceeded limits, showing tail]\n{text}"
    return text


def register_command_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="run_command",
        handler=run_command,
        action="command",
        path_args=("cwd",),
        command_args=("command",),
        risk="high",
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
