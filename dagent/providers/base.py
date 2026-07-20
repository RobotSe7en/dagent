"""Provider interfaces shared by runtime agent loops."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StructuredOutputFormat:
    """Provider-neutral JSON Schema response contract."""

    name: str
    schema: dict[str, Any]
    description: str = ""
    strict: bool = True


@dataclass(frozen=True)
class ChatResponse:
    content: str = ""
    reasoning_content: str = ""
    refusal: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class ChatStreamEvent:
    type: Literal["token", "done"]
    content: str = ""
    channel: Literal["reasoning", "content"] = "content"
    response: ChatResponse | None = None


class ChatProvider(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: StructuredOutputFormat | None = None,
    ) -> ChatResponse:
        """Return the next assistant response."""

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: StructuredOutputFormat | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        """Stream assistant response tokens and finish with a ChatResponse."""
