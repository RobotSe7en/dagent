"""In-container entrypoint for sandboxed built-in tool execution.

Runs inside the Docker sandbox. Imports the *real* tool implementations via
``create_file_tool_registry`` and dispatches a single tool call, so there is no
duplicated tool logic. Reads a base64-encoded JSON request ``{"tool", "args"}``
from ``argv[1]`` and prints a single JSON line ``{"ok", "content", "value"}`` or
``{"ok": false, "error", "error_type"}`` to stdout.

Imports only stdlib + the stdlib-only tool modules. Must NOT import
``boundary.py`` (it pulls pydantic via ``dagent.schemas``).
"""

from __future__ import annotations

import base64
import json
import sys
from typing import Any

from dagent.capabilities.tools.file_tools import create_file_tool_registry
from dagent.capabilities.tools.registry import ToolOutput


def _content_and_value(result: Any) -> tuple[str, Any]:
    if isinstance(result, ToolOutput):
        return result.content, result.value
    if result is None:
        return "", None
    if isinstance(result, str):
        return result, None
    if isinstance(result, bytes):
        return result.decode("utf-8", errors="replace"), None
    if isinstance(result, tuple):
        value = list(result)
        return json.dumps(value, ensure_ascii=False), value
    return str(result), None


def run_request(request: dict[str, Any]) -> dict[str, Any]:
    tool_name = request["tool"]
    args = request.get("args") or {}
    registry = create_file_tool_registry()
    tool = registry.get(tool_name)
    if tool is None:
        return {"ok": False, "error": f"Tool '{tool_name}' is not registered.", "error_type": "RuntimeError"}
    try:
        result = tool.handler(**args)
    except Exception as exc:  # noqa: BLE001 - surfaced to host as structured failure
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
    content, value = _content_and_value(result)
    return {"ok": True, "content": content, "value": value}


def main(argv: list[str]) -> int:
    request = json.loads(base64.b64decode(argv[1]).decode("utf-8"))
    result = run_request(request)
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
