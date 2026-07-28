"""OpenAI-compatible chat provider."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from dagent.config import ProviderConfig
from dagent.providers.base import (
    ChatResponse,
    ChatStreamEvent,
    StructuredOutputFormat,
    ToolCall,
    separate_reasoning_tags,
)
from dagent.schemas.context import ModelTokenUsage


class OpenAICompatibleProvider:
    """Provider for OpenAI-compatible `/v1/chat/completions` endpoints."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.config = config
        self.context_window_tokens = config.context_window_tokens
        self.output_reserve_tokens = config.output_reserve_tokens
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
        content, reasoning_content = _captured_response_content(
            message.content or "",
            _reasoning_content(message),
            capture=_reasoning_capture(self.config),
        )
        tool_calls = [
            _convert_tool_call(tool_call)
            for tool_call in (message.tool_calls or [])
        ]
        return ChatResponse(
            content=content,
            reasoning_content=reasoning_content,
            refusal=str(getattr(message, "refusal", None) or ""),
            tool_calls=tool_calls,
            usage=_model_token_usage(getattr(response, "usage", None)),
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
        token_usage: ModelTokenUsage | None = None
        capture_tag_reasoning = (
            _reasoning_capture(self.config) == "field_and_tags"
        )
        thinking_parser = _ThinkingStreamParser(enabled=True)
        async for chunk in stream:
            chunk_usage = _model_token_usage(getattr(chunk, "usage", None))
            if chunk_usage is not None:
                token_usage = chunk_usage
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
                for channel, part in thinking_parser.feed(str(content)):
                    if channel == "reasoning":
                        if capture_tag_reasoning:
                            reasoning_parts.append(part)
                    else:
                        content_parts.append(part)
                    if part and (channel != "reasoning" or capture_tag_reasoning):
                        yield ChatStreamEvent(
                            type="token",
                            channel=channel,
                            content=part,
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

        for channel, part in thinking_parser.finish():
            if channel == "reasoning":
                if capture_tag_reasoning:
                    reasoning_parts.append(part)
            else:
                content_parts.append(part)
            if part and (channel != "reasoning" or capture_tag_reasoning):
                yield ChatStreamEvent(
                    type="token",
                    channel=channel,
                    content=part,
                )
        content = "".join(content_parts)
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
                usage=token_usage,
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
            if self.config.stream_include_usage:
                kwargs["stream_options"] = {"include_usage": True}
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
            kwargs["response_format"] = {"type": "json_object"}
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
        reasoning: dict[str, Any] | None = None,
        stream_include_usage: bool = False,
        context_window_tokens: int = 32768,
        output_reserve_tokens: int = 4096,
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
                reasoning=reasoning,
                stream_include_usage=stream_include_usage,
                context_window_tokens=context_window_tokens,
                output_reserve_tokens=output_reserve_tokens,
                extra_request_args=extra_request_args or {},
                extra_body=extra_body or {},
            ),
            client=client,
        )


def _parse_tool_arguments(raw_arguments: str | None) -> dict[str, Any]:
    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Model tool-call arguments are not valid JSON.") from exc
    if not isinstance(arguments, dict):
        raise ValueError("Model tool-call arguments must decode to a JSON object.")
    return arguments


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


def _reasoning_capture(config: ProviderConfig) -> str:
    return config.reasoning.capture if config.reasoning is not None else "field_and_tags"


def _captured_response_content(
    content: str,
    reasoning: str,
    *,
    capture: str,
) -> tuple[str, str]:
    return separate_reasoning_tags(
        content,
        reasoning,
        capture_tag_reasoning=capture == "field_and_tags",
    )


def _model_token_usage(value: Any) -> ModelTokenUsage | None:
    if value is None:
        return None
    input_tokens = int(
        getattr(value, "prompt_tokens", None)
        or getattr(value, "input_tokens", None)
        or 0
    )
    output_tokens = int(
        getattr(value, "completion_tokens", None)
        or getattr(value, "output_tokens", None)
        or 0
    )
    details = (
        getattr(value, "completion_tokens_details", None)
        or getattr(value, "output_tokens_details", None)
    )
    reasoning_tokens = int(getattr(details, "reasoning_tokens", None) or 0)
    total_tokens = int(
        getattr(value, "total_tokens", None)
        or input_tokens + output_tokens
    )
    return ModelTokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
    )


class _ThinkingStreamParser:
    """Split think tags correctly even when delimiters span stream chunks."""

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.in_reasoning = False
        self.buffer = ""
        self.strip_content_whitespace = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        if not self.enabled:
            return [("content", chunk)]
        self.buffer += chunk
        output: list[tuple[str, str]] = []
        while self.buffer:
            delimiter = self._CLOSE if self.in_reasoning else self._OPEN
            index = self.buffer.lower().find(delimiter)
            channel = "reasoning" if self.in_reasoning else "content"
            if index >= 0:
                if index:
                    self._append(output, channel, self.buffer[:index])
                self.buffer = self.buffer[index + len(delimiter):]
                self.in_reasoning = not self.in_reasoning
                if not self.in_reasoning:
                    self.strip_content_whitespace = True
                continue
            keep = min(len(self.buffer), len(delimiter) - 1)
            emit_length = len(self.buffer) - keep
            if emit_length:
                self._append(output, channel, self.buffer[:emit_length])
                self.buffer = self.buffer[emit_length:]
            break
        return output

    def finish(self) -> list[tuple[str, str]]:
        if not self.buffer:
            return []
        channel = "reasoning" if self.in_reasoning else "content"
        value = self.buffer
        self.buffer = ""
        output: list[tuple[str, str]] = []
        self._append(output, channel, value)
        return output

    def _append(
        self,
        output: list[tuple[str, str]],
        channel: str,
        value: str,
    ) -> None:
        if channel == "content" and self.strip_content_whitespace:
            value = value.lstrip()
            if value:
                self.strip_content_whitespace = False
        if value:
            output.append((channel, value))
