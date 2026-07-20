"""Deterministic chat provider for tests and early development."""

from __future__ import annotations

from collections import deque
from typing import Any, AsyncIterator

from dagent.providers.base import ChatResponse, ChatStreamEvent, StructuredOutputFormat


class MockProvider:
    """Returns queued responses and records requests."""

    def __init__(self, responses: list[ChatResponse] | None = None) -> None:
        self._responses = deque(responses or [])
        self.requests: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: StructuredOutputFormat | None = None,
    ) -> ChatResponse:
        self.requests.append({
            "messages": list(messages),
            "tools": tools or [],
            "response_format": response_format,
        })
        if not self._responses:
            return ChatResponse(content="")
        return self._responses.popleft()

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: StructuredOutputFormat | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        response = await self.chat(
            messages,
            tools=tools,
            response_format=response_format,
        )
        if response.reasoning_content:
            yield ChatStreamEvent(
                type="token",
                content=response.reasoning_content,
                channel="reasoning",
            )
        if response.content:
            yield ChatStreamEvent(type="token", content=response.content)
        yield ChatStreamEvent(type="done", response=response)
