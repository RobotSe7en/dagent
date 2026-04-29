"""Deterministic chat provider for tests and early development."""

from __future__ import annotations

from collections import deque
from typing import Any

from dagent.providers.base import ChatResponse


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

