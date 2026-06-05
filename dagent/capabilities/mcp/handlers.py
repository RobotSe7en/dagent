"""Capability handlers for MCP tools."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from dagent.schemas import CapabilityInvocation, CapabilityResult

from .errors import sanitize_error


def make_mcp_tool_handler(
    manager: Any,
    *,
    server_name: str,
    tool_name: str,
    timeout_seconds: float = 60.0,
):
    async def execute(invocation: CapabilityInvocation) -> CapabilityResult:
        try:
            result = await asyncio.to_thread(
                manager.call_tool_blocking,
                server_name,
                tool_name,
                dict(invocation.arguments),
                timeout_seconds,
            )
        except Exception as exc:
            return _failed(invocation, sanitize_error(exc), "mcp_call_error")
        if bool(getattr(result, "isError", False)):
            return _failed(invocation, _mcp_content_text(result) or "MCP tool returned an error.", "mcp_error")
        return _completed(invocation, _mcp_result_content(result), _mcp_artifacts(result), _mcp_result_value(result))

    return execute


def _mcp_result_content(result: Any) -> str:
    text = _mcp_content_text(result)
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        return text
    return json.dumps(
        {"result": text if text else structured, "structuredContent": structured},
        ensure_ascii=False,
    )


def _mcp_result_value(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    return _mcp_content_text(result)


def _mcp_content_text(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None) is not None:
            parts.append(str(getattr(block, "text")))
        elif isinstance(block, dict) and block.get("type") == "text" and block.get("text") is not None:
            parts.append(str(block["text"]))
    return "\n".join(parts)


def _mcp_artifacts(result: Any) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for block in getattr(result, "content", None) or []:
        block_type = getattr(block, "type", None) if not isinstance(block, dict) else block.get("type")
        if block_type not in {"image", "audio"}:
            continue
        mime_type = getattr(block, "mimeType", None) if not isinstance(block, dict) else block.get("mimeType")
        data = getattr(block, "data", None) if not isinstance(block, dict) else block.get("data")
        artifacts.append({"type": block_type, "mime_type": mime_type, "data": data})
    return artifacts


def _completed(
    invocation: CapabilityInvocation,
    content: str,
    artifacts: list[dict[str, Any]],
    value: Any,
) -> CapabilityResult:
    return CapabilityResult.completed(
        invocation,
        content,
        value=value,
        artifacts=artifacts,
        policy_decision=invocation.boundary.policy_decision(),
    )


def _failed(invocation: CapabilityInvocation, error: str, stop_reason: str) -> CapabilityResult:
    return CapabilityResult.failed(
        invocation,
        error,
        stop_reason=stop_reason,
        policy_decision=invocation.boundary.policy_decision(),
    )
