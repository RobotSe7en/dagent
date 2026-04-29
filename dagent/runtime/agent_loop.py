"""Bounded node-level agent loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from dagent.providers import ChatProvider, ChatResponse, ToolCall
from dagent.schemas import Boundary
from dagent.tools.executor import ToolExecutor


@dataclass(frozen=True)
class AgentLoopResult:
    final_response: str
    messages: list[dict[str, Any]]
    steps: int
    completed: bool
    stop_reason: str


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
    ) -> AgentLoopResult:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1.")

        loop_messages = list(messages or [])
        loop_messages.append({"role": "user", "content": user_message})

        for step in range(1, max_steps + 1):
            response = await self.provider.chat(
                loop_messages,
                tools=self._tool_definitions_for_boundary(boundary, allowed_tools),
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
                )

            for tool_call in response.tool_calls:
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
