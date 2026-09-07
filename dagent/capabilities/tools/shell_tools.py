"""Shell tools for bounded execution."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path

from dagent.capabilities.tools.registry import ToolRegistry, ToolOutput
from dagent.capabilities.tools.output_capture import collect_output


SHELL_OUTPUT_MAX_LINES = 200
SHELL_OUTPUT_MAX_BYTES = 100_000
SHELL_TRUNCATION_HEADER = "[TRUNCATED] output exceeded limits; showing tail\n"
SHELL_TERMINATION_GRACE_SECONDS = 0.5


class ShellExecutionError(RuntimeError):
    """Execution failure with the independently retained output."""

    def __init__(self, message: str, *, output: ToolOutput | None = None) -> None:
        self.output = output
        super().__init__(message)


def shell(
    command: str, cwd: str | Path = ".", timeout_seconds: int = 30, *,
    _dagent_cancel_event: threading.Event | None = None,
    _dagent_result_context: dict | None = None,
) -> ToolOutput:
    cwd_path = Path(cwd)
    if not cwd_path.is_dir():
        raise ShellExecutionError(f"Working directory does not exist: {cwd_path}")
    context = _dagent_result_context or {
        "workspace": str(cwd_path.resolve()), "runtime_directory": ".dagent",
        "max_bytes": 64 * 1024 * 1024,
    }
    platform_options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                        if os.name == "nt" else {"start_new_session": True})
    process = subprocess.Popen(
        command, cwd=cwd_path, shell=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, bufsize=0, **platform_options,
    )

    def stop() -> None:
        if os.name == "nt":
            _kill_windows_process_tree(process)
        else:
            _signal_posix_process_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=SHELL_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            _signal_posix_process_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=SHELL_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    try:
        output, reason = collect_output(
            process, workspace=Path(context["workspace"]),
            runtime_directory=context["runtime_directory"], max_bytes=context["max_bytes"],
            timeout=timeout_seconds, cancel=_dagent_cancel_event, stop=stop,
        )
    except BaseException:
        stop()
        raise
    content = _tail_truncate(output.content)
    reference = output.content_reference
    if reference is not None:
        reference = {**reference, "preview": content}
    output = ToolOutput(content, retention=output.retention, content_reference=reference)
    if process.returncode != 0 or reason:
        raise ShellExecutionError(content, output=output)
    return output


def _signal_posix_process_group(
    process: subprocess.Popen[str],
    sig: signal.Signals,
) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
    except OSError:
        if process.poll() is None:
            process.send_signal(sig)


def _kill_windows_process_tree(process: subprocess.Popen[str]) -> None:
    """Force-stop the Windows process tree rooted at the shell process."""
    taskkill: subprocess.Popen[str] | None = None
    try:
        taskkill = subprocess.Popen(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        taskkill.communicate(timeout=SHELL_TERMINATION_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        if taskkill is not None and taskkill.poll() is None:
            taskkill.kill()
    finally:
        if process.poll() is None:
            process.kill()




def _tail_truncate(output: str) -> str:
    """Keep the tail of oversized output; shell command endings carry the signal."""
    lines = output.splitlines()
    truncated = False
    if len(lines) > SHELL_OUTPUT_MAX_LINES:
        lines = lines[-SHELL_OUTPUT_MAX_LINES:]
        truncated = True
    text = "\n".join(lines)
    encoded = text.encode("utf-8")
    if truncated or len(encoded) > SHELL_OUTPUT_MAX_BYTES:
        budget = SHELL_OUTPUT_MAX_BYTES - len(SHELL_TRUNCATION_HEADER.encode("utf-8"))
        if len(encoded) > budget:
            text = _decode_utf8_tail(encoded, budget)
        truncated = True
    if truncated:
        text = f"{SHELL_TRUNCATION_HEADER}{text}"
    return text


def _decode_utf8_tail(encoded: bytes, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    tail = encoded[-max_bytes:]
    for index in range(min(4, len(tail) + 1)):
        try:
            return tail[index:].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return tail.decode("utf-8", errors="ignore")


def register_shell_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="shell",
        handler=shell,
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
