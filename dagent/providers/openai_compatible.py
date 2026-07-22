"""OpenAI-compatible chat provider."""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from dagent.config import ProviderConfig, StructuredOutputMode
from dagent.providers.base import (
    ChatResponse,
    ChatStreamEvent,
    StructuredOutputFormat,
    ToolCall,
)


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
        *,
        response_format: StructuredOutputFormat | None = None,
    ) -> ChatResponse:
        kwargs = self._completion_kwargs(
            messages,
            tools=tools,
            response_format=response_format,
        )

        response = await self.client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        content = message.content or ""
        reasoning_content = _reasoning_content(message)
        if self.config.strip_thinking:
            content = _strip_think_blocks(content)
        tool_calls = [
            _convert_tool_call(tool_call)
            for tool_call in (message.tool_calls or [])
        ]
        return ChatResponse(
            content=content,
            reasoning_content=reasoning_content,
            refusal=str(getattr(message, "refusal", None) or ""),
            tool_calls=tool_calls,
        )

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: StructuredOutputFormat | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        kwargs = self._completion_kwargs(
            messages,
            tools=tools,
            response_format=response_format,
            stream=True,
        )

        stream = await self.client.chat.completions.create(**kwargs)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        refusal_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, Any]] = {}
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            reasoning_content = _reasoning_content(delta)
            if reasoning_content:
                reasoning_parts.append(reasoning_content)
                yield ChatStreamEvent(
                    type="token",
                    channel="reasoning",
                    content=reasoning_content,
                )
            content = getattr(delta, "content", None) or ""
            if content:
                content_parts.append(content)
                yield ChatStreamEvent(
                    type="token",
                    channel="content",
                    content=content,
                )
            refusal = getattr(delta, "refusal", None) or ""
            if refusal:
                refusal_parts.append(str(refusal))
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
                reasoning_content="".join(reasoning_parts),
                refusal="".join(refusal_parts),
                tool_calls=[
                    _convert_streamed_tool_call(part)
                    for _, part in sorted(tool_call_parts.items())
                    if part["name"]
                ],
            ),
        )

    def _completion_kwargs(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: StructuredOutputFormat | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
        }
        if stream:
            kwargs["stream"] = True
        if tools:
            kwargs["tools"] = tools

        generated_extra_body: dict[str, Any] = {}
        reasoning = self.config.reasoning
        if reasoning is not None:
            if reasoning.effort is not None:
                kwargs["reasoning_effort"] = reasoning.effort
            if reasoning.budget_tokens is not None:
                kwargs["thinking_token_budget"] = reasoning.budget_tokens
            if reasoning.enabled is not None:
                generated_extra_body["thinking"] = {
                    "type": "enabled" if reasoning.enabled else "disabled",
                }

        extra_body = _merge_dicts(generated_extra_body, self.config.extra_body)
        if extra_body:
            kwargs["extra_body"] = extra_body
        kwargs.update(self.config.extra_request_args)
        if response_format is not None:
            if self.config.structured_output_mode == "json_object":
                kwargs["response_format"] = {"type": "json_object"}
            else:
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": response_format.name,
                        "description": response_format.description,
                        "schema": response_format.schema,
                        "strict": response_format.strict,
                    },
                }
        return kwargs


class Provider(OpenAICompatibleProvider):
    """Public OpenAI-compatible provider SDK entrypoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float = 60,
        strip_thinking: bool = False,
        structured_output_mode: StructuredOutputMode = "json_schema",
        reasoning: dict[str, Any] | None = None,
        extra_request_args: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        super().__init__(
            ProviderConfig(
                base_url=base_url,
                model=model,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_seconds=timeout_seconds,
                strip_thinking=strip_thinking,
                structured_output_mode=structured_output_mode,
                reasoning=reasoning,
                extra_request_args=extra_request_args or {},
                extra_body=extra_body or {},
            ),
            client=client,
        )


def _parse_tool_arguments(raw_arguments: str | None) -> dict[str, Any]:
    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return arguments if isinstance(arguments, dict) else {}


def _convert_tool_call(tool_call: Any) -> ToolCall:
    return ToolCall(
        id=tool_call.id,
        name=tool_call.function.name,
        arguments=_parse_tool_arguments(tool_call.function.arguments),
    )


def _convert_streamed_tool_call(tool_call: dict[str, str]) -> ToolCall:
    return ToolCall(
        id=tool_call["id"],
        name=tool_call["name"],
        arguments=_parse_tool_arguments(tool_call["arguments"]),
    )


def _reasoning_content(item: Any) -> str:
    reasoning = (
        getattr(item, "reasoning_content", None)
        or getattr(item, "reasoning", None)
        or ""
    )
    return str(reasoning)


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _strip_think_blocks(content: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()
