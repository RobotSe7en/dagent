"""Shell tools for bounded execution."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from dagent.capabilities.tools.registry import ToolRegistry


SHELL_OUTPUT_MAX_LINES = 200
SHELL_OUTPUT_MAX_BYTES = 100_000
SHELL_TRUNCATION_HEADER = "[TRUNCATED] output exceeded limits; showing tail\n"
SHELL_TERMINATION_GRACE_SECONDS = 0.5


class ShellExecutionError(RuntimeError):
    """Raised when a shell tool exits unsuccessfully."""


def shell(
    command: str,
    cwd: str | Path = ".",
    timeout_seconds: int = 30,
    *,
    _dagent_cancel_event: threading.Event | None = None,
) -> str:
    cwd_path = Path(cwd)
    if not cwd_path.is_dir():
        raise ShellExecutionError(f"Working directory does not exist: {cwd_path}")

    platform_options: dict[str, object]
    if os.name == "nt":
        platform_options = {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    else:
        platform_options = {"start_new_session": True}

    process = subprocess.Popen(
        command,
        cwd=cwd_path,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        **platform_options,
    )
    try:
        stdout, stderr = _communicate(
            process,
            timeout_seconds=timeout_seconds,
            cancellation_event=_dagent_cancel_event,
        )
    except _ShellCancelled:
        stdout, stderr = _stop_timed_out_process_group(process)
        output = _format_output(stdout, stderr)
        message = "cancelled by caller"
        if output:
            message = f"{message}\n{_tail_truncate(output)}"
        raise ShellExecutionError(message) from None
    except subprocess.TimeoutExpired:
        stdout, stderr = _stop_timed_out_process_group(process)
        output = _format_output(stdout, stderr)
        message = f"timed out after {timeout_seconds} seconds"
        if output:
            message = f"{message}\n{_tail_truncate(output)}"
        raise ShellExecutionError(message) from None

    output = _format_output(stdout, stderr)
    output = _tail_truncate(output)
    formatted = (
        f"exit_code={process.returncode}\n{output}"
        if output
        else f"exit_code={process.returncode}"
    )
    if process.returncode != 0:
        raise ShellExecutionError(formatted)
    return formatted


class _ShellCancelled(Exception):
    pass


def _communicate(
    process: subprocess.Popen[str],
    *,
    timeout_seconds: int,
    cancellation_event: threading.Event | None,
) -> tuple[str, str]:
    if cancellation_event is None:
        return process.communicate(timeout=timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    while True:
        if cancellation_event.is_set():
            raise _ShellCancelled
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout_seconds)
        try:
            return process.communicate(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            continue


def _stop_timed_out_process_group(
    process: subprocess.Popen[str],
) -> tuple[str, str]:
    """Stop a timed-out shell and every child that still owns its pipes."""
    if os.name == "nt":
        _kill_windows_process_tree(process)
    else:
        _signal_posix_process_group(process, signal.SIGTERM)
        try:
            return process.communicate(timeout=SHELL_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _signal_posix_process_group(process, signal.SIGKILL)

    try:
        return process.communicate(timeout=SHELL_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        # A command can deliberately escape its process group while retaining an
        # inherited pipe. Close our pipe readers so such a process cannot keep
        # the SDK call alive beyond the bounded cleanup period.
        stdout = _timeout_output(exc.stdout)
        stderr = _timeout_output(exc.stderr)
        _close_process_pipes(process)
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=SHELL_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        return stdout, stderr


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


def _close_process_pipes(process: subprocess.Popen[str]) -> None:
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            pipe.close()


def _timeout_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def _format_output(stdout: str, stderr: str) -> str:
    output = "\n".join(
        part
        for part in [stdout.strip(), stderr.strip()]
        if part
    )
    return output


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
