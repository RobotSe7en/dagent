"""Provider interfaces shared by runtime agent loops."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, AsyncIterator, Literal, Protocol

from dagent.schemas.context import ModelTokenUsage


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
    usage: ModelTokenUsage | None = None


@dataclass(frozen=True)
class ChatStreamEvent:
    type: Literal["token", "done"]
    content: str = ""
    channel: Literal["reasoning", "content"] = "content"
    response: ChatResponse | None = None


def normalize_chat_response(
    response: ChatResponse,
    *,
    capture_tag_reasoning: bool = True,
) -> ChatResponse:
    """Keep embedded thinking tags out of visible assistant content."""

    content, reasoning = separate_reasoning_tags(
        response.content,
        response.reasoning_content,
        capture_tag_reasoning=capture_tag_reasoning,
    )
    if content == response.content and reasoning == response.reasoning_content:
        return response
    return replace(response, content=content, reasoning_content=reasoning)


def separate_reasoning_tags(
    content: str,
    reasoning: str = "",
    *,
    capture_tag_reasoning: bool = True,
) -> tuple[str, str]:
    """Split complete or unterminated ``<think>`` blocks case-insensitively."""

    lower = content.lower()
    if "<think>" not in lower:
        return content, reasoning

    visible_parts: list[str] = []
    reasoning_parts: list[str] = [reasoning] if reasoning else []
    cursor = 0
    while True:
        open_index = lower.find("<think>", cursor)
        if open_index < 0:
            visible_parts.append(content[cursor:])
            break
        visible_parts.append(content[cursor:open_index])
        reasoning_start = open_index + len("<think>")
        close_index = lower.find("</think>", reasoning_start)
        if close_index < 0:
            if capture_tag_reasoning:
                reasoning_parts.append(content[reasoning_start:])
            break
        if capture_tag_reasoning:
            reasoning_parts.append(content[reasoning_start:close_index])
        cursor = close_index + len("</think>")

    return "".join(visible_parts).strip(), "".join(reasoning_parts)


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
