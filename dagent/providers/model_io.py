"""Provider-neutral, request-scoped model input and output contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, TypeAlias

from dagent.providers.base import (
    ChatProvider,
    ChatResponse,
    StructuredOutputFormat,
    ToolCall,
    separate_reasoning_tags,
)
from dagent.schemas.context import ModelCallMetadata, ModelTokenUsage


@dataclass(frozen=True)
class ModelUserInput:
    source_id: str
    content: str


@dataclass(frozen=True)
class ModelAssistantTurn:
    source_id: str
    content: str = ""
    reasoning: str = ""
    refusal: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ModelToolResultInput:
    source_id: str
    call_id: str
    name: str
    content: str


ModelInputItem: TypeAlias = (
    ModelUserInput | ModelAssistantTurn | ModelToolResultInput
)


@dataclass(frozen=True)
class ModelRequest:
    instructions: str
    items: tuple[ModelInputItem, ...]
    tools: tuple[dict[str, Any], ...] = ()
    response_format: StructuredOutputFormat | None = None


@dataclass(frozen=True)
class ModelResponse:
    content: str = ""
    reasoning: str = ""
    refusal: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: ModelTokenUsage | None = None
    status: str = "completed"
    metadata: ModelCallMetadata | None = None

    @property
    def reasoning_content(self) -> str:
        """Compatibility accessor used by the existing runtime audit paths."""

        return self.reasoning


@dataclass(frozen=True)
class ModelStreamEvent:
    type: Literal["token", "done"]
    content: str = ""
    channel: Literal["reasoning", "content"] = "content"
    response: ModelResponse | None = None


@dataclass(frozen=True)
class ModelTokenCount:
    count: int
    max_model_len: int | None = None
    estimator: Literal["vllm", "heuristic", "custom"] = "vllm"


def normalize_model_response(
    response: ModelResponse,
    *,
    capture_tag_reasoning: bool = True,
) -> ModelResponse:
    content, reasoning = separate_reasoning_tags(
        response.content,
        response.reasoning,
        capture_tag_reasoning=capture_tag_reasoning,
    )
    if content == response.content and reasoning == response.reasoning:
        return response
    return replace(response, content=content, reasoning=reasoning)


def model_response_from_chat(response: ChatResponse) -> ModelResponse:
    return ModelResponse(
        content=response.content,
        reasoning=response.reasoning_content,
        refusal=response.refusal,
        tool_calls=tuple(response.tool_calls),
        usage=response.usage,
        metadata=response.metadata,
    )


def chat_response_from_model(response: ModelResponse) -> ChatResponse:
    return ChatResponse(
        content=response.content,
        reasoning_content=response.reasoning,
        refusal=response.refusal,
        tool_calls=list(response.tool_calls),
        usage=response.usage,
        metadata=response.metadata,
    )


def model_request_to_chat(
    request: ModelRequest,
    *,
    reasoning_field: Literal["reasoning", "reasoning_content", "omit"] = "omit",
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if request.instructions:
        messages.append({"role": "system", "content": request.instructions})
    for item in request.items:
        if isinstance(item, ModelUserInput):
            messages.append({"role": "user", "content": item.content})
            continue
        if isinstance(item, ModelAssistantTurn):
            message: dict[str, Any] = {
                "role": "assistant",
                "content": item.content,
            }
            if item.reasoning and reasoning_field != "omit":
                message[reasoning_field] = item.reasoning
            if item.refusal:
                message["refusal"] = item.refusal
            if item.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                    for call in item.tool_calls
                ]
            messages.append(message)
            continue
        messages.append(
            {
                "role": "tool",
                "tool_call_id": item.call_id,
                "name": item.name,
                "content": item.content,
            }
        )
    return messages


def model_request_to_responses_input(
    request: ModelRequest,
) -> list[dict[str, Any]]:
    response_input: list[dict[str, Any]] = []
    for item in request.items:
        if isinstance(item, ModelUserInput):
            response_input.append({"role": "user", "content": item.content})
            continue
        if isinstance(item, ModelAssistantTurn):
            if item.reasoning:
                response_input.append(
                    {
                        "type": "reasoning",
                        "id": _wire_id("rs", item.source_id),
                        "summary": [],
                        "content": [
                            {"type": "reasoning_text", "text": item.reasoning}
                        ],
                    }
                )
            message_content: list[dict[str, Any]] = []
            if item.content:
                message_content.append(
                    {
                        "type": "output_text",
                        "text": item.content,
                        "annotations": [],
                    }
                )
            if item.refusal:
                message_content.append(
                    {"type": "refusal", "refusal": item.refusal}
                )
            if message_content:
                response_input.append(
                    {
                        "type": "message",
                        "id": _wire_id("msg", item.source_id),
                        "role": "assistant",
                        "status": "completed",
                        "content": message_content,
                    }
                )
            for call in item.tool_calls:
                response_input.append(
                    {
                        "type": "function_call",
                        "id": _wire_id("fc", f"{item.source_id}:{call.id}"),
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                )
            continue
        response_input.append(
            {
                "type": "function_call_output",
                "call_id": item.call_id,
                "output": item.content,
            }
        )
    return response_input


def responses_tools(
    tools: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if tool.get("type") == "function" else None
        if not isinstance(function, dict):
            output.append(dict(tool))
            continue
        converted: dict[str, Any] = {
            "type": "function",
            "name": str(function.get("name") or ""),
            "parameters": function.get("parameters") or {"type": "object"},
        }
        if function.get("description"):
            converted["description"] = function["description"]
        if "strict" in function:
            converted["strict"] = function["strict"]
        output.append(converted)
    return output


def chat_messages_to_model_request(
    messages: Sequence[dict[str, Any]],
    *,
    tools: Sequence[dict[str, Any]] = (),
    response_format: StructuredOutputFormat | None = None,
) -> ModelRequest:
    instructions: list[str] = []
    items: list[ModelInputItem] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        source_id = str(message.get("id") or f"legacy_{index}")
        if role in {"system", "developer"}:
            instructions.append(str(message.get("content") or ""))
        elif role == "user":
            items.append(
                ModelUserInput(
                    source_id=source_id,
                    content=str(message.get("content") or ""),
                )
            )
        elif role == "assistant":
            calls: list[ToolCall] = []
            for raw_call in message.get("tool_calls") or []:
                function = raw_call.get("function") or {}
                raw_arguments = function.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw_arguments)
                except (TypeError, json.JSONDecodeError):
                    arguments = {}
                calls.append(
                    ToolCall(
                        id=str(raw_call.get("id") or ""),
                        name=str(function.get("name") or ""),
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )
            items.append(
                ModelAssistantTurn(
                    source_id=source_id,
                    content=str(message.get("content") or ""),
                    reasoning=str(
                        message.get("reasoning")
                        or message.get("reasoning_content")
                        or ""
                    ),
                    refusal=str(message.get("refusal") or ""),
                    tool_calls=tuple(calls),
                )
            )
        elif role == "tool":
            items.append(
                ModelToolResultInput(
                    source_id=source_id,
                    call_id=str(message.get("tool_call_id") or ""),
                    name=str(message.get("name") or ""),
                    content=str(message.get("content") or ""),
                )
            )
    return ModelRequest(
        instructions="\n\n".join(part for part in instructions if part),
        items=tuple(items),
        tools=tuple(dict(tool) for tool in tools),
        response_format=response_format,
    )


async def complete_model(
    provider: ChatProvider,
    request: ModelRequest,
) -> ModelResponse:
    complete = getattr(provider, "complete", None)
    if callable(complete):
        return await complete(request)
    response = await provider.chat(
        model_request_to_chat(request, reasoning_field="omit"),
        tools=list(request.tools) or None,
        response_format=request.response_format,
    )
    return model_response_from_chat(response)


async def stream_model(
    provider: ChatProvider,
    request: ModelRequest,
) -> AsyncIterator[ModelStreamEvent]:
    stream = getattr(provider, "stream", None)
    if callable(stream):
        async for event in stream(request):
            yield event
        return
    stream_chat = getattr(provider, "stream_chat", None)
    if callable(stream_chat):
        async for event in stream_chat(
            model_request_to_chat(request, reasoning_field="omit"),
            tools=list(request.tools) or None,
            response_format=request.response_format,
        ):
            yield ModelStreamEvent(
                type=event.type,
                content=event.content,
                channel=event.channel,
                response=(
                    None
                    if event.response is None
                    else model_response_from_chat(event.response)
                ),
            )
        return
    yield ModelStreamEvent(type="done", response=await complete_model(provider, request))


def _wire_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


__all__ = [
    "ModelAssistantTurn",
    "ModelCallMetadata",
    "ModelInputItem",
    "ModelRequest",
    "ModelResponse",
    "ModelStreamEvent",
    "ModelTokenCount",
    "ModelToolResultInput",
    "ModelUserInput",
    "chat_messages_to_model_request",
    "chat_response_from_model",
    "complete_model",
    "model_request_to_chat",
    "model_request_to_responses_input",
    "model_response_from_chat",
    "normalize_model_response",
    "responses_tools",
    "stream_model",
]
