"""Bounded tool-using agent loop."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4

from dagent.harness_runtime.dag_builder import strip_thinking_blocks
from dagent.harness_runtime.review_policy import effective_risk, review_policy
from dagent.providers import ChatProvider, ChatResponse, ToolCall
from dagent.schemas import Boundary, LoopOutcome, PendingReview, ToolInvocation
from dagent.tools.boundary import BoundaryViolation
from dagent.tools.executor import ToolExecutor
from dagent.tools.registry import Tool
import dagent.harness_runtime.review_policy as _rp


MAX_EXECUTION_CONTEXT_CHARS = 16000
MAX_TOOL_RESULT_CONTEXT_CHARS = 4000


@dataclass(frozen=True)
class ControlToolResult:
    """Result returned by a harness-level control tool."""

    content: str
    stop_reason: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    needs_review: bool = False
    review_payload: dict[str, Any] | None = None


ControlToolHandler = Callable[[ToolCall], Awaitable[ControlToolResult]]
TokenHandler = Callable[[str], None]
LoopEventHandler = Callable[[dict[str, Any]], None]


class ToolAgentLoop:
    """Runs one bounded tool-using agent loop."""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        tool_executor: ToolExecutor,
    ) -> None:
        self.provider = provider
        self.tool_executor = tool_executor

    def create_tool_guard(
        self,
        review_level: "_rp.ReviewLevel",
        boundary: Boundary,
        tools: list[Tool],
    ) -> ControlToolHandler:
        policy = review_policy(review_level)

        async def guard(tool_call: ToolCall) -> ControlToolResult:
            tool_obj = next((t for t in tools if t.name == tool_call.name), None)
            risk = effective_risk(tool_obj, tool_call.arguments)
            if not policy.reviews_tool(risk):
                try:
                    result_content = self.tool_executor.execute(
                        tool_call.name, tool_call.arguments, boundary=boundary
                    )
                except Exception as exc:
                    return ControlToolResult(
                        content=f"[TOOL_ERROR] {type(exc).__name__}: {exc}",
                    )
                return ControlToolResult(content=result_content)
            return ControlToolResult(
                content=f"[PENDING_REVIEW] Tool '{tool_call.name}' requires human review (risk={risk}).",
                needs_review=True,
                review_payload={"tool_name": tool_call.name, "risk": risk},
            )

        return guard

    async def run(
        self,
        user_message: str,
        *,
        boundary: Boundary,
        max_steps: int = 8,
        allowed_tools: list[str] | None = None,
        messages: list[dict[str, Any]] | None = None,
        extra_tools: list[dict[str, Any]] | None = None,
        control_tool_names: set[str] | None = None,
        control_tool_handler: ControlToolHandler | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> LoopOutcome:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1.")

        loop_messages = list(messages or [])
        if user_message:
            loop_messages.append({"role": "user", "content": user_message})
        control_events: list[dict[str, Any]] = []
        invocations: list[ToolInvocation] = []

        for step in range(1, max_steps + 1):
            tool_definitions = [
                *self._tool_definitions_for_boundary(boundary, allowed_tools),
                *(extra_tools or []),
            ]
            response = await self._chat(
                loop_messages,
                tools=tool_definitions,
                on_token=on_token,
            )

            assistant_message = self._assistant_message(response)
            loop_messages.append(assistant_message)

            if not response.tool_calls:
                return LoopOutcome(
                    status="completed",
                    execution_context=_format_tool_execution_context(loop_messages),
                    final_answer=strip_thinking_blocks(response.content).strip(),
                    messages=loop_messages,
                    events=control_events,
                    invocations=invocations,
                )

            for tool_call in response.tool_calls:
                tool_obj = self.tool_executor.registry.get(tool_call.name)
                invocation = ToolInvocation(
                    invocation_id=tool_call.id,
                    tool_name=tool_call.name,
                    arguments=tool_call.arguments,
                    boundary=boundary,
                    risk=effective_risk(tool_obj, tool_call.arguments),
                )
                invocations.append(invocation)
                self._emit_tool_event(on_event, tool_call, "tool_call")
                if control_tool_names and tool_call.name in control_tool_names:
                    if control_tool_handler is None:
                        raise ValueError(f"Control tool '{tool_call.name}' has no handler.")
                    try:
                        control_result = await control_tool_handler(tool_call)
                    except Exception as exc:
                        self._emit_tool_event(on_event, tool_call, "tool_error", content=str(exc))
                        raise
                    control_events.extend(control_result.events)
                    loop_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": control_result.content,
                        }
                    )
                    self._emit_tool_event(
                        on_event,
                        tool_call,
                        "tool_result",
                        content=control_result.content,
                    )
                    if control_result.needs_review:
                        pending_review = PendingReview(
                            review_id=f"review_{uuid4().hex}",
                            kind="tool_review",
                            message=f"Review tool call: {tool_call.name}",
                            tool_call={
                                "tool_call_id": tool_call.id,
                                "name": tool_call.name,
                                "arguments": tool_call.arguments,
                            },
                            payload=control_result.review_payload or {},
                        )
                        return LoopOutcome(
                            status="awaiting_review",
                            execution_context=_format_tool_execution_context(loop_messages),
                            messages=loop_messages,
                            events=control_events,
                            invocations=invocations,
                            pending_review=pending_review,
                        )
                    if control_result.stop_reason:
                        return LoopOutcome(
                            status="failed",
                            execution_context=_format_tool_execution_context(loop_messages),
                            final_answer=control_result.content,
                            messages=loop_messages,
                            events=control_events,
                            invocations=invocations,
                        )
                    continue

                if allowed_tools is not None and tool_call.name not in allowed_tools:
                    error_content = f"[ERROR] Tool '{tool_call.name}' is not allowed. Available tools: {', '.join(allowed_tools)}."
                    self._emit_tool_event(on_event, tool_call, "tool_error", content=error_content)
                    loop_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": error_content,
                        }
                    )
                    continue
                try:
                    tool_result = self.tool_executor.execute(
                        tool_call.name,
                        tool_call.arguments,
                        boundary=boundary,
                    )
                except BoundaryViolation as exc:
                    error_content = (
                        f"[BOUNDARY_VIOLATION] {exc}\n"
                        f"Tool '{tool_call.name}' exceeded the current boundary. "
                        "Adjust your arguments to stay within allowed paths/commands, "
                        "or use an alternative approach."
                    )
                    self._emit_tool_event(on_event, tool_call, "tool_error", content=error_content)
                    loop_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": error_content,
                        }
                    )
                    continue
                except Exception as exc:
                    error_content = f"[TOOL_ERROR] {type(exc).__name__}: {exc}"
                    self._emit_tool_event(on_event, tool_call, "tool_error", content=error_content)
                    loop_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": error_content,
                        }
                    )
                    continue
                loop_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": tool_result,
                    }
                )
                self._emit_tool_event(on_event, tool_call, "tool_result", content=tool_result)

        return LoopOutcome(
            status="failed",
            execution_context=_format_tool_execution_context(loop_messages),
            messages=loop_messages,
            events=control_events,
            invocations=invocations,
        )

    def _tool_definitions_for_boundary(
        self,
        boundary: Boundary,
        allowed_tools: list[str] | None,
    ) -> list[dict[str, Any]]:
        allowed = set(allowed_tools) if allowed_tools is not None else None
        tool_names = sorted(self.tool_executor.registry.names())
        definitions: list[dict[str, Any]] = []
        for name in tool_names:
            if allowed is not None and name not in allowed:
                continue
            tool = self.tool_executor.registry.get(name)
            if tool is None:
                continue
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters or {"type": "object"},
                    },
                }
            )
        return definitions

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        on_token: TokenHandler | None,
    ) -> ChatResponse:
        if on_token is None or not hasattr(self.provider, "stream_chat"):
            return await self.provider.chat(messages, tools=tools)

        response: ChatResponse | None = None
        async for event in self.provider.stream_chat(messages, tools=tools):
            if event.type == "token" and event.content:
                on_token(event.content)
            elif event.type == "done":
                response = event.response
        return response or ChatResponse()

    def _assistant_message(self, response: ChatResponse) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": response.content,
        }
        if response.tool_calls:
            message["tool_calls"] = [
                self._tool_call_message(tool_call) for tool_call in response.tool_calls
            ]
        return message

    def _tool_call_message(self, tool_call: ToolCall) -> dict[str, Any]:
        return {
            "id": tool_call.id,
            "type": "function",
            "function": {
                "name": tool_call.name,
                "arguments": json.dumps(tool_call.arguments),
            },
        }

    def _emit_tool_event(
        self,
        on_event: LoopEventHandler | None,
        tool_call: ToolCall,
        event_type: str,
        *,
        content: str | None = None,
    ) -> None:
        if on_event is None:
            return
        payload: dict[str, Any] = {
            "type": event_type,
            "tool_call_id": tool_call.id,
            "name": tool_call.name,
            "arguments": tool_call.arguments,
        }
        if content is not None:
            payload["content"] = content
        on_event(payload)


def _format_tool_execution_context(messages: list[dict[str, Any]]) -> str:
    """Format ToolAgentLoop tool calls for validation and fallback output."""
    lines: list[str] = []
    for message in messages:
        if message.get("role") == "tool":
            name = message.get("name", "unknown")
            content = _context_excerpt(
                str(message.get("content", "")),
                limit=MAX_TOOL_RESULT_CONTEXT_CHARS,
            )
            lines.append(f"  - {name}: {content}")

    if lines:
        return _context_excerpt(
            "Tool call results:\n" + "\n".join(lines),
            limit=MAX_EXECUTION_CONTEXT_CHARS,
        )

    final_answer = _last_assistant_content(messages)
    if final_answer:
        return _context_excerpt(
            f"Assistant response:\n{final_answer}",
            limit=MAX_EXECUTION_CONTEXT_CHARS,
        )
    return ""


def _last_assistant_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content"):
            return str(message["content"])
    return ""


def _context_excerpt(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[TRUNCATED after {limit} chars]"
