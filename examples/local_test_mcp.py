"""Small local stdio MCP server for testing dagent MCP registration.

Run from the repository root:

    uv run python -m examples.local_test_mcp

When configuring it in the WebUI, use command ``uv`` with args:

    run
    python
    -m
    examples.local_test_mcp

Set cwd to this repository root if the WebUI process is not already running
from here. Do not print regular logs to stdout in this file; stdio MCP uses
stdout for JSON-RPC frames.
"""

from __future__ import annotations

import os
import platform
import time
from typing import Any

SERVER_NAME = "dagent-local-test-mcp"
STARTED_AT = time.monotonic()
ECHO_DELAY_SECONDS = 130.0
MAX_DELAY_SECONDS = 5.0


def ping(message: str = "hello") -> dict[str, Any]:
    """Return a small payload proving the MCP server is alive."""

    return {
        "ok": True,
        "server": SERVER_NAME,
        "message": message,
        "pid": os.getpid(),
        "uptime_seconds": round(time.monotonic() - STARTED_AT, 3),
    }


def echo(text: str, uppercase: bool = False) -> str:
    """Echo text back after a long delay, optionally uppercased."""

    time.sleep(ECHO_DELAY_SECONDS)
    return text.upper() if uppercase else text


def add(a: float, b: float) -> float:
    """Add two numbers and return the numeric sum."""

    return a + b


def env_check(name: str, reveal: bool = False) -> dict[str, Any]:
    """Report whether an environment variable is visible to the MCP process.

    Values are only returned when ``reveal`` is true and the variable name starts
    with ``DAGENT_TEST_``. This keeps accidental secret exposure out of normal
    chat transcripts while still allowing intentional test variables.
    """

    value = os.environ.get(name)
    can_reveal = reveal and name.startswith("DAGENT_TEST_")
    return {
        "name": name,
        "present": value is not None,
        "length": 0 if value is None else len(value),
        "value": value if value is not None and can_reveal else None,
    }


def server_info() -> dict[str, Any]:
    """Return process and working-directory details for MCP debugging."""

    return {
        "server": SERVER_NAME,
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "uptime_seconds": round(time.monotonic() - STARTED_AT, 3),
    }


def delayed_echo(text: str, delay_seconds: float = 0.25) -> dict[str, Any]:
    """Echo text after a bounded delay to test MCP call timeouts."""

    delay = clamp_delay(delay_seconds)
    if delay:
        time.sleep(delay)
    return {
        "text": text,
        "delay_seconds": delay,
    }


def clamp_delay(delay_seconds: float) -> float:
    """Clamp a requested delay into the safe diagnostic range."""

    return min(MAX_DELAY_SECONDS, max(0.0, float(delay_seconds)))


def build_server() -> Any:
    """Build the FastMCP server lazily so tests can import helpers without MCP."""

    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        SERVER_NAME,
        instructions=(
            "Diagnostic local MCP server for dagent. Use ping, echo, add, "
            "env_check, server_info, and delayed_echo to verify stdio MCP "
            "registration and tool calls."
        ),
    )
    server.tool()(ping)
    server.tool()(echo)
    server.tool()(add)
    server.tool()(env_check)
    server.tool()(server_info)
    server.tool()(delayed_echo)
    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
