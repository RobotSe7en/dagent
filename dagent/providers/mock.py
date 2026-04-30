"""Deterministic chat provider for tests and early development."""

from __future__ import annotations

from collections import deque
from typing import Any, AsyncIterator

from dagent.providers.base import ChatResponse, ChatStreamEvent


class MockProvider:
    """Returns queued responses and records requests."""

    def __init__(self, responses: list[ChatResponse] | None = None) -> None:
        self._responses = deque(responses or [])
        self.requests: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        self.requests.append({"messages": list(messages), "tools": tools or []})
        if not self._responses:
            return ChatResponse(content="")
        return self._responses.popleft()

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        response = await self.chat(messages, tools=tools)
        if response.content:
            yield ChatStreamEvent(type="token", content=response.content)
        yield ChatStreamEvent(type="done", response=response)

