"""Bounded node-level agent loop."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from dagent.providers import ChatProvider, ChatResponse, ToolCall
from dagent.schemas import Boundary
from dagent.tools.executor import ToolExecutor


@dataclass(frozen=True)
class ControlToolResult:
    """Result returned by a harness-level control tool."""

    content: str
    stop_reason: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class AgentLoopResult:
    final_response: str
    messages: list[dict[str, Any]]
    steps: int
    completed: bool
    stop_reason: str
    control_events: list[dict[str, Any]] = field(default_factory=list)


ControlToolHandler = Callable[[ToolCall], Awaitable[ControlToolResult]]
TokenHandler = Callable[[str], None]


class AgentLoop:
    """Runs one bounded agent loop for a DAG node."""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        tool_executor: ToolExecutor,
    ) -> None:
        self.provider = provider
        self.tool_executor = tool_executor

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
    ) -> AgentLoopResult:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1.")

        loop_messages = list(messages or [])
        if user_message:
            loop_messages.append({"role": "user", "content": user_message})
        control_events: list[dict[str, Any]] = []

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
                return AgentLoopResult(
                    final_response=response.content,
                    messages=loop_messages,
                    steps=step,
                    completed=True,
                    stop_reason="completed",
                    control_events=control_events,
                )

            for tool_call in response.tool_calls:
                if control_tool_names and tool_call.name in control_tool_names:
                    if control_tool_handler is None:
                        raise ValueError(f"Control tool '{tool_call.name}' has no handler.")
                    control_result = await control_tool_handler(tool_call)
                    control_events.extend(control_result.events)
                    loop_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": control_result.content,
                        }
                    )
                    if control_result.stop_reason:
                        return AgentLoopResult(
                            final_response=control_result.content,
                            messages=loop_messages,
                            steps=step,
                            completed=False,
                            stop_reason=control_result.stop_reason,
                            control_events=control_events,
                        )
                    continue

                if allowed_tools is not None and tool_call.name not in allowed_tools:
                    raise ValueError(f"Tool '{tool_call.name}' is not allowed for this node.")
                tool_result = self.tool_executor.execute(
                    tool_call.name,
                    tool_call.arguments,
                    boundary=boundary,
                )
                loop_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": tool_result,
                    }
                )

        return AgentLoopResult(
            final_response="",
            messages=loop_messages,
            steps=max_steps,
            completed=False,
            stop_reason="max_steps",
            control_events=control_events,
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
            if name in boundary.forbidden_tools:
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
