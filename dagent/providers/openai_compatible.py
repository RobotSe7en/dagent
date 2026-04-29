"""OpenAI-compatible chat provider."""

from __future__ import annotations

import json
import re
from typing import Any

from openai import AsyncOpenAI

from dagent.config import ProviderConfig
from dagent.providers.base import ChatResponse, ToolCall


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


def _strip_think_blocks(content: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()
