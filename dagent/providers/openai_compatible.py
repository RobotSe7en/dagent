"""OpenAI-compatible chat provider."""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from dagent.config import ProviderConfig
from dagent.providers.base import ChatResponse, ChatStreamEvent, ToolCall


class OpenAICompatibleProvider:
    """Provider for OpenAI-compatible `/v1/chat/completions` endpoints."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.config = config
        self.client = client or AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        response = await self.client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        content = message.content or ""
        if self.config.strip_thinking:
            content = _strip_think_blocks(content)
        tool_calls = [
            _convert_tool_call(tool_call)
            for tool_call in (message.tool_calls or [])
        ]
        return ChatResponse(content=content, tool_calls=tool_calls)

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools

        stream = await self.client.chat.completions.create(**kwargs)
        content_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, Any]] = {}
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None) or ""
            if content:
                content_parts.append(content)
                yield ChatStreamEvent(type="token", content=content)
            for tool_call in getattr(delta, "tool_calls", None) or []:
                index = int(getattr(tool_call, "index", 0) or 0)
                part = tool_call_parts.setdefault(
                    index,
                    {"id": "", "name": "", "arguments": ""},
                )
                if getattr(tool_call, "id", None):
                    part["id"] += tool_call.id
                function = getattr(tool_call, "function", None)
                if function is not None:
                    if getattr(function, "name", None):
                        part["name"] += function.name
                    if getattr(function, "arguments", None):
                        part["arguments"] += function.arguments

        content = "".join(content_parts)
        if self.config.strip_thinking:
            content = _strip_think_blocks(content)
        yield ChatStreamEvent(
            type="done",
            response=ChatResponse(
                content=content,
                tool_calls=[
                    _convert_streamed_tool_call(part)
                    for _, part in sorted(tool_call_parts.items())
                    if part["name"]
                ],
            ),
        )


def _convert_tool_call(tool_call: Any) -> ToolCall:
    raw_arguments = tool_call.function.arguments or "{}"
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}

    return ToolCall(
        id=tool_call.id,
        name=tool_call.function.name,
        arguments=arguments,
    )


def _convert_streamed_tool_call(tool_call: dict[str, str]) -> ToolCall:
    raw_arguments = tool_call["arguments"] or "{}"
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}

    return ToolCall(
        id=tool_call["id"],
        name=tool_call["name"],
        arguments=arguments,
    )


def _strip_think_blocks(content: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()
